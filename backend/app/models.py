import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    BigInteger,
    Float,
    DateTime,
    ForeignKey,
    Text,
    Index
)
from sqlalchemy.orm import relationship
from backend.app.database import Base

def utc_now():
    return datetime.now(timezone.utc)

class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    source_filename = Column(String(255), nullable=False)
    source_checksum = Column(String(64), nullable=False, index=True) # SHA-256
    schema_version = Column(String(32), default="1.0.0", nullable=False)
    file_size_bytes = Column(BigInteger, default=0, nullable=False)
    storage_path = Column(String(512), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    jobs = relationship("IngestionJob", back_populates="dataset", cascade="all, delete-orphan")
    aggregate_versions = relationship("AggregateVersion", back_populates="dataset", cascade="all, delete-orphan")

class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id = Column(String(36), ForeignKey("datasets.id"), nullable=False, index=True)
    status = Column(String(32), default="PENDING", nullable=False, index=True) # PENDING, RUNNING, COMPLETED, FAILED, CANCELED
    progress_percent = Column(Float, default=0.0, nullable=False)
    rows_total = Column(Integer, default=0, nullable=False)
    rows_accepted = Column(Integer, default=0, nullable=False)
    rows_quarantined = Column(Integer, default=0, nullable=False)
    processed_bytes = Column(BigInteger, default=0, nullable=False)
    rows_per_second = Column(Float, default=0.0, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    dataset = relationship("Dataset", back_populates="jobs")
    validation_errors = relationship("ValidationError", back_populates="job", cascade="all, delete-orphan")
    aggregate_version = relationship("AggregateVersion", back_populates="job", uselist=False)

class ValidationError(Base):
    __tablename__ = "validation_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("ingestion_jobs.id"), nullable=False, index=True)
    row_number = Column(Integer, nullable=False)
    event_id = Column(String(255), nullable=True)
    error_code = Column(String(64), nullable=False, index=True)
    error_message = Column(Text, nullable=False)
    raw_payload = Column(Text, nullable=True)

    job = relationship("IngestionJob", back_populates="validation_errors")

class AggregateVersion(Base):
    __tablename__ = "aggregate_versions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id = Column(String(36), ForeignKey("datasets.id"), nullable=False, index=True)
    job_id = Column(String(36), ForeignKey("ingestion_jobs.id"), nullable=False, unique=True)
    version_number = Column(Integer, nullable=False)
    status = Column(String(32), default="BUILDING", nullable=False, index=True) # BUILDING, PUBLISHED, ARCHIVED
    grand_total_amount_minor = Column(BigInteger, default=0, nullable=False)
    category_summaries_json = Column(Text, nullable=False) # JSON string: {CAT: {count, total_amount_minor}}
    quarantined_total = Column(Integer, default=0, nullable=False)
    accepted_total = Column(Integer, default=0, nullable=False)
    published_at = Column(DateTime, nullable=True)

    dataset = relationship("Dataset", back_populates="aggregate_versions")
    job = relationship("IngestionJob", back_populates="aggregate_version")

    __table_args__ = (
        Index("idx_dataset_version", "dataset_id", "version_number", unique=True),
    )

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(32), default="PENDING", nullable=False, index=True) # PENDING, PUBLISHED, FAILED
    retry_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    published_at = Column(DateTime, nullable=True)
