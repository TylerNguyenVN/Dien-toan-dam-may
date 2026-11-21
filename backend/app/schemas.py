from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, ConfigDict

class DatasetResponse(BaseModel):
    id: str
    name: str
    source_filename: str
    source_checksum: str
    schema_version: str
    file_size_bytes: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class IngestionJobResponse(BaseModel):
    id: str
    dataset_id: str
    status: str
    progress_percent: float
    rows_total: int
    rows_accepted: int
    rows_quarantined: int
    rows_per_second: float
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class CategoryMetric(BaseModel):
    count: int
    total_amount_minor: int

class DashboardResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    version_number: int
    version_id: str
    status: str
    source_checksum: str
    schema_version: str
    published_at: Optional[datetime] = None
    accepted_total: int
    quarantined_total: int
    grand_total_amount_minor: int
    categories: Dict[str, CategoryMetric]

class SystemMetricsResponse(BaseModel):
    uptime_seconds: float
    total_datasets: int
    total_jobs: int
    total_rows_processed: int
    active_jobs: int
    avg_throughput_rows_sec: float
