import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.app.config import settings
from backend.app.models import Dataset, IngestionJob, ValidationError, AggregateVersion, utc_now
from backend.app.validator import RowValidator, ALLOWED_CATEGORIES

class IngestionEngine:
    def __init__(self, db: Session):
        self.db = db

    def process_job(self, job_id: str, fail_at_row: Optional[int] = None) -> dict:
        """
        Processes an ingestion job synchronously or inside a background worker.
        Supports atomic publishing and simulated failure injection for failure drills.
        """
        job = self.db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
        if not job:
            raise ValueError(f"IngestionJob {job_id} not found.")

        # Idempotency check: if job is already completed, do not re-run or double count
        if job.status == "COMPLETED":
            return {
                "job_id": job.id,
                "status": "COMPLETED",
                "message": "Job already completed (idempotent no-op)."
            }

        dataset = self.db.query(Dataset).filter(Dataset.id == job.dataset_id).first()
        if not dataset:
            raise ValueError(f"Dataset {job.dataset_id} not found.")

        csv_path = Path(dataset.storage_path)
        if not csv_path.exists():
            job.status = "FAILED"
            job.error_message = f"CSV file not found at {csv_path}"
            self.db.commit()
            raise FileNotFoundError(f"File not found: {csv_path}")

        # Determine next version number for this dataset
        latest_ver = (
            self.db.query(AggregateVersion)
            .filter(AggregateVersion.dataset_id == dataset.id)
            .order_by(AggregateVersion.version_number.desc())
            .first()
        )
        next_version_num = (latest_ver.version_number + 1) if latest_ver else 1

        # Create or reuse AggregateVersion in BUILDING state
        agg_version = self.db.query(AggregateVersion).filter(AggregateVersion.job_id == job.id).first()
        if not agg_version:
            agg_version = AggregateVersion(
                dataset_id=dataset.id,
                job_id=job.id,
                version_number=next_version_num,
                status="BUILDING",
                grand_total_amount_minor=0,
                category_summaries_json="{}",
                quarantined_total=0,
                accepted_total=0
            )
            self.db.add(agg_version)

        job.status = "RUNNING"
        job.started_at = utc_now()
        self.db.commit()

        start_perf_time = time.perf_counter()
        validator = RowValidator()

        category_counts = {cat: 0 for cat in ALLOWED_CATEGORIES}
        category_sums = {cat: 0 for cat in ALLOWED_CATEGORIES}
        grand_total_amount = 0
        accepted_count = 0
        quarantined_count = 0
        processed_bytes = 0

        validation_errors_to_create = []

        try:
            with open(csv_path, mode="r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                
                # Check header columns
                expected_headers = {"event_id", "category", "timestamp_utc", "amount_minor"}
                if not reader.fieldnames or not expected_headers.issubset(set(reader.fieldnames)):
                    raise ValueError(f"CSV header missing required columns. Got: {reader.fieldnames}")

                row_idx = 0
                for row in reader:
                    row_idx += 1
                    
                    # Simulated crash injection for Failure Drill requirement
                    if fail_at_row is not None and row_idx >= fail_at_row:
                        job.status = "FAILED"
                        job.error_message = f"Injected crash drill at row {row_idx}"
                        job.finished_at = utc_now()
                        self.db.commit()
                        raise RuntimeError(f"Failure drill: Worker abruptly crashed midway at row {row_idx}.")

                    is_valid, err_code, err_msg, amount_val, cat = validator.validate_row(row, row_idx)

                    if is_valid:
                        accepted_count += 1
                        category_counts[cat] += 1
                        category_sums[cat] += amount_val
                        grand_total_amount += amount_val
                    else:
                        quarantined_count += 1
                        validation_errors_to_create.append(
                            ValidationError(
                                job_id=job.id,
                                row_number=row_idx,
                                event_id=row.get("event_id"),
                                error_code=err_code,
                                error_message=err_msg,
                                raw_payload=json.dumps(row)
                            )
                        )

                    # Update progress periodically
                    if row_idx % 20000 == 0:
                        job.rows_total = row_idx
                        job.rows_accepted = accepted_count
                        job.rows_quarantined = quarantined_count
                        self.db.commit()

            # Batch insert validation errors if any (up to 5,000 to keep DB lean)
            if validation_errors_to_create:
                self.db.bulk_save_objects(validation_errors_to_create[:5000])

            elapsed = max(time.perf_counter() - start_perf_time, 0.001)
            rows_per_second = row_idx / elapsed
            processed_bytes = csv_path.stat().st_size

            # Build category summary JSON
            cat_summary = {
                cat: {
                    "count": category_counts[cat],
                    "total_amount_minor": category_sums[cat]
                }
                for cat in ALLOWED_CATEGORIES
            }

            # Atomic transition to PUBLISHED
            agg_version.grand_total_amount_minor = grand_total_amount
            agg_version.category_summaries_json = json.dumps(cat_summary)
            agg_version.accepted_total = accepted_count
            agg_version.quarantined_total = quarantined_count
            agg_version.status = "PUBLISHED"
            agg_version.published_at = utc_now()

            # Complete job
            job.status = "COMPLETED"
            job.progress_percent = 100.0
            job.rows_total = row_idx
            job.rows_accepted = accepted_count
            job.rows_quarantined = quarantined_count
            job.processed_bytes = processed_bytes
            job.rows_per_second = round(rows_per_second, 2)
            job.finished_at = utc_now()

            self.db.commit()

            # Write downloadable result file
            summary_output_path = settings.STORAGE_DIR / f"result_summary_{dataset.id}_v{agg_version.version_number}.json"
            summary_data = {
                "dataset_id": dataset.id,
                "dataset_name": dataset.name,
                "version_number": agg_version.version_number,
                "schema_version": dataset.schema_version,
                "source_checksum": dataset.source_checksum,
                "published_at": agg_version.published_at.isoformat(),
                "metrics": {
                    "rows_total": row_idx,
                    "rows_accepted": accepted_count,
                    "rows_quarantined": quarantined_count,
                    "grand_total_amount_minor": grand_total_amount,
                    "category_summaries": cat_summary
                }
            }
            with open(summary_output_path, "w", encoding="utf-8") as sf:
                json.dump(summary_data, sf, indent=2)

            return {
                "job_id": job.id,
                "version_id": agg_version.id,
                "version_number": agg_version.version_number,
                "status": "COMPLETED",
                "rows_total": row_idx,
                "rows_accepted": accepted_count,
                "rows_quarantined": quarantined_count,
                "grand_total_amount_minor": grand_total_amount,
                "rows_per_second": job.rows_per_second,
                "result_download_path": str(summary_output_path)
            }

        except Exception as ex:
            self.db.rollback()
            job.status = "FAILED"
            job.error_message = str(ex)
            job.finished_at = utc_now()
            self.db.commit()
            raise
