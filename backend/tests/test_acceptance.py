import json
import shutil
from pathlib import Path
import pytest

from backend.app.database import engine, Base, SessionLocal
from backend.app.models import Dataset, IngestionJob, AggregateVersion, ValidationError
from backend.app.ingestion import IngestionEngine
from backend.app.config import settings

@pytest.fixture(scope="module")
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    # Cleanup after tests
    # Base.metadata.drop_all(bind=engine)

def test_1_exact_agreement_benchmark_100k(setup_db):
    """
    Test 1 from project brief:
    Compare counts and integer sums with an independently generated reference result: exact agreement.
    """
    db = SessionLocal()
    try:
        csv_path = Path("data/benchmark_100k.csv")
        metrics_path = Path("data/benchmark_100k_metrics.json")
        assert csv_path.exists(), "Benchmark CSV 100k must exist"
        assert metrics_path.exists(), "Benchmark metrics JSON must exist"

        with open(metrics_path, "r", encoding="utf-8") as f:
            expected = json.load(f)

        # 1. Create Dataset record
        dataset = Dataset(
            name="Benchmark 100k Transactions",
            source_filename="benchmark_100k.csv",
            source_checksum=expected["metadata"]["source_sha256"],
            schema_version="1.0.0",
            file_size_bytes=csv_path.stat().st_size,
            storage_path=str(csv_path.resolve())
        )
        db.add(dataset)
        db.commit()

        # 2. Create IngestionJob
        job = IngestionJob(
            dataset_id=dataset.id,
            status="PENDING"
        )
        db.add(job)
        db.commit()

        # 3. Process Job through Engine
        engine_runner = IngestionEngine(db)
        result = engine_runner.process_job(job.id)

        # 4. Strict assertions against independently generated metrics
        exp_summary = expected["validation_summary"]
        exp_agg = expected["aggregate_results"]

        assert result["status"] == "COMPLETED"
        assert result["rows_total"] == exp_summary["total_rows"]
        assert result["rows_accepted"] == exp_summary["accepted_rows"]
        assert result["rows_quarantined"] == exp_summary["quarantined_rows"]
        assert result["grand_total_amount_minor"] == exp_agg["grand_total_amount_minor"]

        # Assert category totals match exactly
        agg_ver = db.query(AggregateVersion).filter(AggregateVersion.job_id == job.id).first()
        assert agg_ver is not None
        assert agg_ver.status == "PUBLISHED"

        stored_cat_summaries = json.loads(agg_ver.category_summaries_json)
        for cat, val in exp_agg["categories"].items():
            assert stored_cat_summaries[cat]["count"] == val["count"], f"Mismatch count for category {cat}"
            assert stored_cat_summaries[cat]["total_amount_minor"] == val["total_amount_minor"], f"Mismatch sum for category {cat}"

        print(f"\n[TEST 1 PASSED] Processed {result['rows_total']:,} rows at {result['rows_per_second']:,} rows/sec!")
        print(f"Grand total amount minor exact match: {result['grand_total_amount_minor']:,}")

    finally:
        db.close()

def test_3_idempotency_and_no_double_counting(setup_db):
    """
    Test 3 from project brief:
    Retry the same job: no double-counting; malformed rows are accounted for in a reconciliation total.
    """
    db = SessionLocal()
    try:
        # Get existing completed job
        job = db.query(IngestionJob).filter(IngestionJob.status == "COMPLETED").first()
        assert job is not None

        engine_runner = IngestionEngine(db)
        retry_result = engine_runner.process_job(job.id)

        assert retry_result["status"] == "COMPLETED"
        assert "idempotent no-op" in retry_result.get("message", "")

        # Verify no duplicate AggregateVersions created
        versions = db.query(AggregateVersion).filter(AggregateVersion.dataset_id == job.dataset_id).all()
        assert len(versions) == 1, "Only one aggregate version should exist for this job"

        print("\n[TEST 3 PASSED] Job retry is fully idempotent, no double-counting.")
    finally:
        db.close()

def test_failure_drill_and_resilient_publishing(setup_db):
    """
    Failure drill from project brief:
    Stop processing midway. The previous published dashboard remains readable and intact.
    """
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).first()
        assert dataset is not None

        # Previous published version
        published_v1 = (
            db.query(AggregateVersion)
            .filter(AggregateVersion.dataset_id == dataset.id, AggregateVersion.status == "PUBLISHED")
            .first()
        )
        assert published_v1 is not None
        v1_grand_total = published_v1.grand_total_amount_minor

        # Start a new job and inject a crash midway at row 30,000
        crash_job = IngestionJob(
            dataset_id=dataset.id,
            status="PENDING"
        )
        db.add(crash_job)
        db.commit()

        engine_runner = IngestionEngine(db)
        with pytest.raises(RuntimeError, match="Worker abruptly crashed"):
            engine_runner.process_job(crash_job.id, fail_at_row=30000)

        # Verify that crash_job is marked as FAILED
        db.refresh(crash_job)
        assert crash_job.status == "FAILED"
        assert "Failure drill" in crash_job.error_message

        # Crucial Requirement: The previous published dashboard remains unaffected and readable!
        db.refresh(published_v1)
        assert published_v1.status == "PUBLISHED"
        assert published_v1.grand_total_amount_minor == v1_grand_total

        print("\n[FAILURE DRILL PASSED] Crash midway contained. Published dashboard version remains completely readable.")
    finally:
        db.close()
