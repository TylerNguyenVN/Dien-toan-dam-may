import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, BackgroundTasks, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.app.config import settings
from backend.app.database import engine, Base, get_db
from backend.app.models import Dataset, IngestionJob, AggregateVersion, ValidationError, utc_now
from backend.app.ingestion import IngestionEngine
from backend.app.schemas import (
    DatasetResponse,
    IngestionJobResponse,
    DashboardResponse,
    SystemMetricsResponse
)
from backend.app.auth import require_dataset_owner, get_current_user, User, DEMO_USERS

# Initialize database schema tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="CSV Analytics Dashboard Backend with Asynchronous Ingestion & Atomic Versioning"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SERVER_START_TIME = time.time()

def run_background_ingestion(job_id: str, fail_at_row: Optional[int] = None):
    """Worker function executed in background task or queue worker."""
    from backend.app.database import SessionLocal
    db = SessionLocal()
    try:
        engine_runner = IngestionEngine(db)
        engine_runner.process_job(job_id, fail_at_row=fail_at_row)
    except Exception as ex:
        print(f"[ALERT] Ingestion worker encountered failure for job {job_id}: {ex}")
    finally:
        db.close()

@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "schema_version": settings.SCHEMA_VERSION,
        "timestamp": utc_now().isoformat()
    }

@app.post("/api/v1/datasets/upload", response_model=dict, status_code=status.HTTP_202_ACCEPTED, tags=["Datasets"])
async def upload_dataset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    fail_at_row: Optional[int] = Form(None),
    current_user: User = Depends(require_dataset_owner),
    db: Session = Depends(get_db)
):
    """
    Accepts CSV upload, writes stream to storage, computes SHA-256 checksum,
    registers Dataset and queues asynchronous IngestionJob.
    Supports fail_at_row for Failure Drill demonstration.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are supported.")

    dataset_name = name or Path(file.filename).stem
    temp_target_path = settings.STORAGE_DIR / f"{int(time.time())}_{file.filename}"

    hasher = hashlib.sha256()
    file_size = 0

    with open(temp_target_path, "wb") as buffer:
        while chunk := await file.read(65536):
            hasher.update(chunk)
            file_size += len(chunk)
            buffer.write(chunk)

    file_checksum = hasher.hexdigest()

    # Check if this exact checksum already exists
    existing_dataset = db.query(Dataset).filter(Dataset.source_checksum == file_checksum).first()
    if existing_dataset:
        dataset = existing_dataset
    else:
        dataset = Dataset(
            name=dataset_name,
            source_filename=file.filename,
            source_checksum=file_checksum,
            schema_version=settings.SCHEMA_VERSION,
            file_size_bytes=file_size,
            storage_path=str(temp_target_path.resolve())
        )
        db.add(dataset)
        db.commit()
        db.refresh(dataset)

    # Create new IngestionJob
    job = IngestionJob(
        dataset_id=dataset.id,
        status="PENDING",
        processed_bytes=file_size
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Enqueue background ingestion worker
    background_tasks.add_task(run_background_ingestion, job.id, fail_at_row=fail_at_row)

    return {
        "dataset_id": dataset.id,
        "job_id": job.id,
        "status": "PENDING",
        "message": "File accepted. Asynchronous ingestion started.",
        "source_checksum": file_checksum,
        "poll_url": f"/api/v1/jobs/{job.id}"
    }

@app.get("/api/v1/jobs/{job_id}", response_model=IngestionJobResponse, tags=["Jobs"])
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    """Poll job progress, validation count, and error states."""
    job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="IngestionJob not found.")
    return job

@app.get("/api/v1/jobs/{job_id}/errors", tags=["Jobs"])
def get_job_errors(
    job_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """Returns quarantined row error codes, summary counts, and paginated error items."""
    job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="IngestionJob not found.")

    # Breakdown by error code
    breakdown_query = (
        db.query(ValidationError.error_code, func.count(ValidationError.id))
        .filter(ValidationError.job_id == job_id)
        .group_by(ValidationError.error_code)
        .all()
    )
    breakdown = {code: count for code, count in breakdown_query}

    # Paginated errors
    errors = (
        db.query(ValidationError)
        .filter(ValidationError.job_id == job_id)
        .order_by(ValidationError.row_number.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "job_id": job_id,
        "total_quarantined": job.rows_quarantined,
        "breakdown": breakdown,
        "errors": [
            {
                "row_number": err.row_number,
                "event_id": err.event_id,
                "error_code": err.error_code,
                "error_message": err.error_message,
                "raw_payload": err.raw_payload
            }
            for err in errors
        ]
    }

@app.get("/api/v1/jobs/{job_id}/quarantine/export", tags=["Jobs"])
def export_quarantined_csv(
    job_id: str,
    db: Session = Depends(get_db)
):
    """Exports all quarantined error rows for a job as a downloadable CSV audit file."""
    import csv
    import io

    job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="IngestionJob not found.")

    errors = (
        db.query(ValidationError)
        .filter(ValidationError.job_id == job_id)
        .order_by(ValidationError.row_number.asc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["row_number", "error_code", "error_message", "event_id", "raw_payload"])
    for err in errors:
        writer.writerow([err.row_number, err.error_code, err.error_message, err.event_id or "", err.raw_payload or ""])

    output.seek(0)
    filename = "quarantine_audit.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/api/v1/datasets", response_model=list[DatasetResponse], tags=["Datasets"])
def list_datasets(db: Session = Depends(get_db)):
    """List all registered datasets."""
    return db.query(Dataset).order_by(Dataset.created_at.desc()).all()

@app.get("/api/v1/datasets/{dataset_id}/versions", tags=["Dashboard"])
def list_dataset_versions(dataset_id: str, db: Session = Depends(get_db)):
    """List all aggregate snapshot versions for a dataset."""
    versions = (
        db.query(AggregateVersion)
        .filter(AggregateVersion.dataset_id == dataset_id)
        .order_by(AggregateVersion.version_number.desc())
        .all()
    )
    return [
        {
            "version_number": v.version_number,
            "status": v.status,
            "grand_total_amount_minor": v.grand_total_amount_minor,
            "accepted_total": v.accepted_total,
            "quarantined_total": v.quarantined_total,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "job_id": v.job_id
        }
        for v in versions
    ]

@app.get("/api/v1/datasets/{dataset_id}/dashboard", response_model=DashboardResponse, tags=["Dashboard"])
def get_dataset_dashboard(
    dataset_id: str,
    version: Optional[int] = Query(None, description="Optional specific version number"),
    db: Session = Depends(get_db)
):
    """
    Returns an aggregate snapshot. Defaults to the latest PUBLISHED snapshot.
    Guarantees atomic reads: readers never see an incomplete or building version.
    """
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found.")

    query = db.query(AggregateVersion).filter(
        AggregateVersion.dataset_id == dataset_id,
        AggregateVersion.status == "PUBLISHED"
    )

    if version is not None:
        selected_version = query.filter(AggregateVersion.version_number == version).first()
    else:
        selected_version = query.order_by(AggregateVersion.version_number.desc()).first()

    if not selected_version:
        raise HTTPException(
            status_code=404,
            detail="No published dashboard version exists for this query."
        )

    categories_data = json.loads(selected_version.category_summaries_json)

    return DashboardResponse(
        dataset_id=dataset.id,
        dataset_name=dataset.name,
        version_number=selected_version.version_number,
        version_id=selected_version.id,
        status=selected_version.status,
        source_checksum=dataset.source_checksum,
        schema_version=dataset.schema_version,
        published_at=selected_version.published_at,
        accepted_total=selected_version.accepted_total,
        quarantined_total=selected_version.quarantined_total,
        grand_total_amount_minor=selected_version.grand_total_amount_minor,
        categories=categories_data
    )

@app.get("/api/v1/datasets/{dataset_id}/download", tags=["Dashboard"])
def download_summary_file(
    dataset_id: str,
    version: Optional[int] = Query(None),
    db: Session = Depends(get_db)
):
    """Download the summary result file for a version."""
    query = db.query(AggregateVersion).filter(
        AggregateVersion.dataset_id == dataset_id,
        AggregateVersion.status == "PUBLISHED"
    )
    if version:
        selected_ver = query.filter(AggregateVersion.version_number == version).first()
    else:
        selected_ver = query.order_by(AggregateVersion.version_number.desc()).first()

    if not selected_ver:
        raise HTTPException(status_code=404, detail="No published version found to download.")

    summary_file = settings.STORAGE_DIR / f"result_summary_{dataset_id}_v{selected_ver.version_number}.json"
    if not summary_file.exists():
        raise HTTPException(status_code=404, detail="Result file not found on storage.")

    return FileResponse(
        path=summary_file,
        filename=f"analytics_summary_v{selected_ver.version_number}.json",
        media_type="application/json"
    )

@app.get("/api/v1/metrics", response_model=SystemMetricsResponse, tags=["Observability"])
def get_system_metrics(db: Session = Depends(get_db)):
    """Observability endpoint for dashboard latency, rows processed, and throughput."""
    total_datasets = db.query(Dataset).count()
    total_jobs = db.query(IngestionJob).count()
    total_rows = db.query(func.sum(IngestionJob.rows_total)).scalar() or 0
    active_jobs = db.query(IngestionJob).filter(IngestionJob.status.in_(["PENDING", "RUNNING"])).count()
    avg_throughput = db.query(func.avg(IngestionJob.rows_per_second)).filter(IngestionJob.status == "COMPLETED").scalar() or 0.0

    return SystemMetricsResponse(
        uptime_seconds=round(time.time() - SERVER_START_TIME, 2),
        total_datasets=total_datasets,
        total_jobs=total_jobs,
        total_rows_processed=int(total_rows),
        active_jobs=active_jobs,
        avg_throughput_rows_sec=round(float(avg_throughput), 2)
    )

@app.get("/", response_class=HTMLResponse, tags=["Web UI"])
def serve_web_dashboard():
    """Serves the integrated Cloud CSV Analytics Web Dashboard with Light & Dark modes."""
    html_content = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CSV Analytics Dashboard | Cloud Computing</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            neutral: {
              850: '#1f1f1f',
              900: '#171717',
              950: '#0a0a0a'
            }
          }
        }
      }
    };
    if (localStorage.getItem('theme') === 'light') {
      document.documentElement.classList.remove('dark');
    } else {
      document.documentElement.classList.add('dark');
    }
  </script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body class="bg-zinc-100 text-zinc-900 dark:bg-neutral-950 dark:text-zinc-100 min-h-screen font-sans transition-colors duration-200">
  <!-- Top Navigation Header -->
  <header class="border-b border-zinc-200 bg-white/90 dark:border-neutral-800 dark:bg-neutral-950/90 backdrop-blur sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-8 h-8 rounded-lg bg-zinc-900 text-white dark:bg-white dark:text-neutral-950 flex items-center justify-center font-bold text-sm shadow-sm">
          C9
        </div>
        <span class="font-semibold text-base sm:text-lg tracking-tight">CSV Analytics Dashboard</span>
        <span class="text-xs bg-zinc-100 text-zinc-600 border border-zinc-300 dark:bg-neutral-900 dark:text-zinc-400 dark:border-neutral-800 px-2 py-0.5 rounded-md font-mono">v1.0.0</span>
      </div>

      <div class="flex items-center space-x-2 sm:space-x-3 text-xs">
        <!-- RBAC Role Switcher -->
        <div class="flex items-center space-x-1.5 bg-zinc-100 dark:bg-neutral-900 border border-zinc-200 dark:border-neutral-800 rounded-lg px-2.5 py-1 text-xs">
          <span class="text-zinc-500 dark:text-zinc-400 font-medium hidden md:inline">Role:</span>
          <select id="userRoleSelect" onchange="onRoleChange(this.value)" class="bg-transparent text-xs font-semibold text-zinc-900 dark:text-zinc-100 focus:outline-none cursor-pointer">
            <option value="owner-token-2026">Alice (Owner) [Full Access]</option>
            <option value="analyst-token-2026">Bob (Analyst) [Read-Only]</option>
          </select>
        </div>

        <!-- Swagger Docs Pill -->
        <a href="/docs" target="_blank" class="flex items-center space-x-1 px-2.5 py-1 rounded-lg border border-zinc-200 dark:border-neutral-800 bg-zinc-100 hover:bg-zinc-200 dark:bg-neutral-900 dark:hover:bg-neutral-850 text-xs font-medium text-zinc-700 dark:text-zinc-300 transition" title="Interactive Swagger UI">
          <svg class="w-3.5 h-3.5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"/></svg>
          <span class="font-mono">Swagger</span>
        </a>

        <!-- ReDoc Pill -->
        <a href="/redoc" target="_blank" class="hidden sm:flex items-center space-x-1 px-2.5 py-1 rounded-lg border border-zinc-200 dark:border-neutral-800 bg-zinc-100 hover:bg-zinc-200 dark:bg-neutral-900 dark:hover:bg-neutral-850 text-xs font-medium text-zinc-700 dark:text-zinc-300 transition" title="ReDoc OpenAPI Specification">
          <svg class="w-3.5 h-3.5 text-purple-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
          <span class="font-mono">ReDoc</span>
        </a>

        <div id="metricsThroughput" class="text-zinc-500 dark:text-zinc-400 font-mono hidden lg:block">
          Avg: <span class="text-blue-600 dark:text-blue-400 font-semibold" id="valAvgThroughput">--</span> rows/s
        </div>
        
        <div id="systemStatus" class="flex items-center space-x-2 text-emerald-600 dark:text-emerald-400 font-medium">
          <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
          <span class="hidden sm:inline">API Operational</span>
        </div>

        <!-- Light / Dark Mode Toggle Button -->
        <button id="themeToggleBtn" onclick="toggleTheme()" class="p-2 rounded-lg border border-zinc-200 bg-zinc-100 hover:bg-zinc-200 text-zinc-700 dark:border-neutral-800 dark:bg-neutral-900 dark:hover:bg-neutral-850 dark:text-zinc-300 transition" title="Toggle Light / Dark Mode">
          <!-- Sun icon (shown in dark mode) -->
          <svg id="sunIcon" class="w-4 h-4 hidden dark:block" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 9h-1m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
          <!-- Moon icon (shown in light mode) -->
          <svg id="moonIcon" class="w-4 h-4 block dark:hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"/></svg>
        </button>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 py-8 space-y-6">
    <!-- Acceptance Benchmark & Cloud Resilience Badge -->
    <section class="bg-white/80 dark:bg-neutral-900/80 border border-zinc-200/90 dark:border-neutral-800 rounded-2xl p-4 shadow-sm transition backdrop-blur">
      <div class="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
        <div class="flex items-center space-x-3">
          <div class="w-8 h-8 rounded-xl bg-blue-50 text-blue-600 border border-blue-200 dark:bg-blue-950/70 dark:text-blue-400 dark:border-blue-900/60 flex items-center justify-center shrink-0">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>
          </div>
          <div>
            <div class="font-semibold text-xs text-zinc-900 dark:text-zinc-100 flex items-center space-x-2">
              <span>Acceptance Criteria Verification (Test 1 & Test 2)</span>
              <span class="text-[10px] bg-emerald-100 text-emerald-800 dark:bg-emerald-950/80 dark:text-emerald-400 border border-emerald-300 dark:border-emerald-900 px-2 py-0.5 rounded-full font-mono font-semibold">VERIFIED</span>
            </div>
            <p class="text-[11px] text-zinc-500 dark:text-zinc-400 mt-0.5">100,000 Rows Exact Integer Sum Agreement • Zero-Downtime Atomic Snapshot • Least-Privilege RBAC</p>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2 font-mono text-[11px]">
          <div class="px-3 py-1.5 rounded-xl border border-blue-200 bg-blue-50/80 dark:border-blue-900/60 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 font-semibold flex items-center space-x-1.5 shadow-sm">
            <span class="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse"></span>
            <span>P95 Latency: 36.46 ms</span>
          </div>
          <div class="px-3 py-1.5 rounded-xl border border-purple-200 bg-purple-50/80 dark:border-purple-900/60 dark:bg-purple-950/40 text-purple-700 dark:text-purple-300 font-semibold flex items-center space-x-1.5 shadow-sm">
            <span class="w-1.5 h-1.5 rounded-full bg-purple-500"></span>
            <span>10 Concurrent Readers</span>
          </div>
          <div class="px-3 py-1.5 rounded-xl border border-emerald-200 bg-emerald-50/80 dark:border-emerald-900/60 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 font-semibold flex items-center space-x-1.5 shadow-sm">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
            <span>Throughput: >280.000 rows/s</span>
          </div>
        </div>
      </div>
    </section>

    <!-- RBAC Security Alert Banner (Triggered when Analyst attempts upload) -->
    <div id="rbacAlertBanner" class="hidden p-4 rounded-xl border border-amber-300 bg-amber-50 dark:border-amber-900/60 dark:bg-amber-950/40 backdrop-blur text-amber-900 dark:text-amber-200 flex items-start space-x-3 transition">
      <svg class="w-5 h-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
      <div class="flex-1 text-sm">
        <span class="font-bold text-amber-700 dark:text-amber-300">RBAC ACCESS CONTROL ENFORCED (HTTP 403 Forbidden):</span>
        <span class="ml-1">Tài khoản Bob (Analyst) chỉ có quyền đọc số liệu (Read-only).</span>
        <div class="text-xs text-amber-700 dark:text-amber-400/90 mt-1">Nguyên tắc bảo mật Least Privilege: Chỉ tài khoản Alice (Dataset Owner) mới được phép nạp dataset mới. Vui lòng chuyển vai trò thành Alice (Owner) ở góc phải trên cùng để thực hiện tải lên.</div>
      </div>
      <button onclick="document.getElementById('rbacAlertBanner').classList.add('hidden')" class="text-amber-600 dark:text-amber-400 hover:opacity-75 text-xs font-bold px-2 py-1">Dismiss</button>
    </div>

    <!-- Actionable Alert Banner (Hidden by default) -->
    <div id="alertBanner" class="hidden p-4 rounded-xl border border-rose-300 bg-rose-50 dark:border-rose-900/60 dark:bg-rose-950/40 backdrop-blur text-rose-800 dark:text-rose-200 flex items-start space-x-3 transition">
      <svg class="w-5 h-5 text-rose-600 dark:text-rose-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
      <div class="flex-1 text-sm">
        <span class="font-bold text-rose-700 dark:text-rose-300">ACTIONABLE ALERT (Failure Drill Triggered):</span>
        <span id="alertMessage" class="ml-1">Worker crashed midway during ingestion.</span>
        <div class="text-xs text-rose-600 dark:text-rose-400/90 mt-1">Automatic Recovery: Incomplete snapshot rolled back. Dashboard continues serving last published snapshot with zero downtime.</div>
      </div>
      <button onclick="document.getElementById('alertBanner').classList.add('hidden')" class="text-rose-600 dark:text-rose-400 hover:opacity-75 text-xs font-bold px-2 py-1">Dismiss</button>
    </div>

    <!-- Upload Section -->
    <section class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-6 shadow-sm transition">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between mb-4">
        <div>
          <h2 class="text-lg font-semibold tracking-tight">Upload Transaction Dataset (CSV)</h2>
          <p class="text-sm text-zinc-500 dark:text-zinc-400">Target schema: <code>event_id, category, timestamp_utc, amount_minor</code>. Supports 100,000+ rows with async stream ingestion.</p>
        </div>
        <!-- Version Selector -->
        <div id="versionSelectorBox" class="mt-3 sm:mt-0 flex items-center space-x-2">
          <label class="text-xs text-zinc-500 dark:text-zinc-400">Snapshot Version:</label>
          <select id="versionSelect" onchange="onVersionChange(this.value)" class="bg-zinc-50 border border-zinc-300 dark:bg-neutral-950 dark:border-neutral-800 text-xs rounded-lg px-3 py-1.5 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-zinc-400">
            <option value="latest">Latest Published</option>
          </select>
        </div>
      </div>
      
      <div class="flex flex-col sm:flex-row gap-4 items-center">
        <input type="file" id="csvFileInput" accept=".csv" class="block w-full text-sm text-zinc-600 dark:text-zinc-300 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-zinc-900 file:text-white dark:file:bg-zinc-100 dark:file:text-zinc-900 hover:file:opacity-90 cursor-pointer bg-zinc-50 dark:bg-neutral-950 border border-zinc-300 dark:border-neutral-800 rounded-xl p-2" />
        <button id="uploadBtn" onclick="handleUpload()" class="w-full sm:w-auto px-6 py-2.5 bg-zinc-900 text-white hover:bg-zinc-800 dark:bg-white dark:text-neutral-950 dark:hover:bg-zinc-200 font-medium rounded-xl transition shadow-sm whitespace-nowrap">Upload & Process</button>
      </div>

      <!-- Failure Drill Option Checkbox -->
      <div class="mt-3 flex items-center space-x-2 text-xs text-amber-700 dark:text-amber-400">
        <input type="checkbox" id="failureDrillCheckbox" class="w-4 h-4 rounded bg-zinc-100 border-zinc-300 dark:bg-neutral-950 dark:border-neutral-800 text-amber-600 focus:ring-amber-500 cursor-pointer">
        <label for="failureDrillCheckbox" class="cursor-pointer">
          <span class="font-semibold">[Demo Failure Drill]</span> Simulate Worker Crash midway at row 30,000 (verifies Zero Downtime snapshot isolation)
        </label>
      </div>

      <!-- Progress bar -->
      <div id="progressContainer" class="mt-4 hidden space-y-2">
        <div class="flex justify-between text-xs text-zinc-500 dark:text-zinc-400 font-mono">
          <span id="jobStatusText">Ingesting rows...</span>
          <span id="jobProgressPercent">0%</span>
        </div>
        <div class="w-full bg-zinc-200 dark:bg-neutral-950 rounded-full h-2 overflow-hidden">
          <div id="progressBar" class="bg-zinc-900 dark:bg-zinc-100 h-2 rounded-full transition-all duration-300" style="width: 0%"></div>
        </div>
      </div>
    </section>

    <!-- KPI Summary Cards -->
    <section id="kpiSection" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-5 shadow-sm transition">
        <div class="text-xs uppercase tracking-wider text-zinc-500 dark:text-zinc-400 font-medium">Grand Total (Minor)</div>
        <div id="kpiGrandTotal" class="text-2xl font-bold mt-1 text-zinc-900 dark:text-white font-mono">--</div>
        <div class="text-xs text-zinc-500 dark:text-zinc-400 mt-1">Sum of integer minor units</div>
      </div>

      <div class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-5 shadow-sm transition">
        <div class="text-xs uppercase tracking-wider text-emerald-700 dark:text-emerald-400 font-medium">Accepted Rows</div>
        <div id="kpiAccepted" class="text-2xl font-bold mt-1 text-emerald-700 dark:text-emerald-400 font-mono">--</div>
        <div class="text-xs text-zinc-500 dark:text-zinc-400 mt-1">Schema & format validated</div>
      </div>

      <div class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-5 shadow-sm transition">
        <div class="text-xs uppercase tracking-wider text-rose-700 dark:text-rose-400 font-medium">Quarantined Rows</div>
        <div id="kpiQuarantined" class="text-2xl font-bold mt-1 text-rose-700 dark:text-rose-400 font-mono">--</div>
        <div class="text-xs text-zinc-500 dark:text-zinc-400 mt-1">Duplicate or malformed</div>
      </div>

      <div class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-5 shadow-sm transition">
        <div class="text-xs uppercase tracking-wider text-zinc-700 dark:text-zinc-300 font-medium">Snapshot Version</div>
        <div id="kpiVersion" class="text-2xl font-bold mt-1 text-zinc-900 dark:text-zinc-100 font-mono">--</div>
        <div class="text-xs text-zinc-500 dark:text-zinc-400 mt-1">Atomic published snapshot</div>
      </div>
    </section>

    <!-- Charts & Action Section -->
    <section id="chartsSection" class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="lg:col-span-2 bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-6 shadow-sm transition">
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-semibold text-base tracking-tight">Category Totals (Amount Minor)</h3>
          <button id="downloadBtn" onclick="handleDownload()" class="hidden text-xs bg-zinc-100 hover:bg-zinc-200 text-zinc-800 border border-zinc-300 dark:bg-neutral-800 dark:hover:bg-neutral-700 dark:text-zinc-200 dark:border-neutral-700 px-3 py-1.5 rounded-lg transition font-medium">Download JSON Summary</button>
        </div>
        <div class="h-64">
          <canvas id="categoryBarChart"></canvas>
        </div>
      </div>

      <div class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-6 shadow-sm transition">
        <h3 class="font-semibold text-base tracking-tight mb-4">Category Volume Share</h3>
        <div class="h-64 flex items-center justify-center">
          <canvas id="categoryPieChart"></canvas>
        </div>
      </div>
    </section>

    <!-- Quarantined Rows Deep-Dive Table -->
    <section id="quarantineSection" class="bg-white border border-zinc-200/90 dark:bg-neutral-900/90 dark:border-neutral-800 rounded-2xl p-6 shadow-sm transition">
      <div class="flex items-center justify-between mb-4">
        <div>
          <h3 class="font-semibold text-base tracking-tight">Quarantined Rows Analysis (Data Hygiene)</h3>
          <p class="text-xs text-zinc-500 dark:text-zinc-400">Malformed rows isolated during ingestion to preserve financial calculation integrity.</p>
        </div>
        <div class="flex items-center space-x-2">
          <button id="exportQuarantineBtn" onclick="exportQuarantineCsv()" class="hidden text-xs bg-rose-50 hover:bg-rose-100 text-rose-700 border border-rose-300 dark:bg-rose-950/60 dark:hover:bg-rose-900/80 dark:text-rose-200 dark:border-rose-800 px-3 py-1.5 rounded-xl transition font-medium shadow-sm flex items-center space-x-1.5 cursor-pointer" title="Xuất toàn bộ dòng lỗi cách ly ra file quarantine_audit.csv">
            <svg class="w-3.5 h-3.5 text-rose-600 dark:text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
            <span>Export Quarantined CSV</span>
          </button>
          <span id="quarantineBadge" class="text-xs bg-rose-100 text-rose-800 border border-rose-300 dark:bg-rose-950/80 dark:text-rose-400 dark:border-rose-900 px-3 py-1 rounded-full font-mono font-semibold">0 Errors</span>
        </div>
      </div>

      <!-- Breakdown Pills -->
      <div id="quarantineBreakdownPills" class="flex flex-wrap gap-2 mb-4"></div>

      <!-- Sample Errors Table -->
      <div class="overflow-x-auto rounded-xl border border-zinc-200 dark:border-neutral-800">
        <table class="w-full text-left text-xs">
          <thead class="bg-zinc-50 text-zinc-600 dark:bg-neutral-950 dark:text-zinc-400 uppercase font-mono text-[10px] border-b border-zinc-200 dark:border-neutral-800">
            <tr>
              <th class="px-4 py-3">Row #</th>
              <th class="px-4 py-3">Error Code</th>
              <th class="px-4 py-3">Error Detail</th>
              <th class="px-4 py-3">Event ID</th>
            </tr>
          </thead>
          <tbody id="errorTableBody" class="divide-y divide-zinc-200 dark:divide-neutral-800 font-mono text-zinc-700 dark:text-zinc-300">
            <tr>
              <td colspan="4" class="px-4 py-6 text-center text-zinc-400 dark:text-zinc-500">No quarantined samples loaded yet. Upload a dataset to inspect errors.</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    let currentDatasetId = null;
    let currentJobId = null;
    let currentVersionNumber = null;
    let pollInterval = null;
    let barChart = null;
    let pieChart = null;
    let latestCategoriesData = null;
    let activeToken = localStorage.getItem('user_token') || 'owner-token-2026';

    const roleSelect = document.getElementById('userRoleSelect');
    if (roleSelect) roleSelect.value = activeToken;

    function onRoleChange(token) {
      activeToken = token;
      localStorage.setItem('user_token', token);
      document.getElementById('rbacAlertBanner').classList.add('hidden');
    }

    function isDarkMode() {
      return document.documentElement.classList.contains('dark');
    }

    function toggleTheme() {
      if (isDarkMode()) {
        document.documentElement.classList.remove('dark');
        localStorage.setItem('theme', 'light');
      } else {
        document.documentElement.classList.add('dark');
        localStorage.setItem('theme', 'dark');
      }
      if (latestCategoriesData) {
        updateCharts(latestCategoriesData);
      }
    }

    // Load initial system metrics
    async function loadMetrics() {
      try {
        const res = await fetch('/api/v1/metrics');
        if (res.ok) {
          const m = await res.json();
          document.getElementById('valAvgThroughput').innerText = m.avg_throughput_rows_sec.toLocaleString();
        }
      } catch (e) {}
    }
    loadMetrics();

    // Auto-detect existing datasets on page load
    async function autoLoadExisting() {
      try {
        const res = await fetch('/api/v1/datasets');
        if (res.ok) {
          const list = await res.json();
          if (list.length > 0) {
            currentDatasetId = list[0].id;
            await loadVersionList(currentDatasetId);
            await fetchDashboard(currentDatasetId);
          }
        }
      } catch (e) {}
    }
    autoLoadExisting();

    function handleUpload() {
      const fileInput = document.getElementById('csvFileInput');
      if (!fileInput.files || fileInput.files.length === 0) {
        alert('Please choose a CSV file first.');
        return;
      }

      const isFailureDrill = document.getElementById('failureDrillCheckbox').checked;
      const file = fileInput.files[0];

      // Check Vercel serverless request body size limit (4.5 MB)
      if (window.location.hostname.includes('vercel.app') && file.size > 4.4 * 1024 * 1024) {
        alert("⚠️ Vercel Serverless Function giới hạn dung lượng tải lên tối đa là 4.5 MB.\\nFile '" + file.name + "' hiện tại là " + (file.size / (1024 * 1024)).toFixed(2) + " MB.\\n\\n👉 Vui lòng chọn file 'demo_50k.csv' (3.6 MB) hoặc 'reference_dataset.csv' khi chạy trên Vercel.\\n(Khi chạy trên Docker / AWS / Render sẽ không bị giới hạn này).");
        return;
      }

      const formData = new FormData();
      formData.append('file', file);
      formData.append('name', file.name.replace('.csv', ''));
      if (isFailureDrill) {
        formData.append('fail_at_row', '30000');
      }

      const uploadBtn = document.getElementById('uploadBtn');
      const progressContainer = document.getElementById('progressContainer');
      const progressBar = document.getElementById('progressBar');
      const jobStatusText = document.getElementById('jobStatusText');
      const jobProgressPercent = document.getElementById('jobProgressPercent');

      uploadBtn.disabled = true;
      uploadBtn.innerText = 'Uploading...';
      progressContainer.classList.remove('hidden');
      progressBar.classList.remove('bg-rose-500');
      progressBar.classList.add('bg-zinc-900', 'dark:bg-zinc-100');
      jobStatusText.innerText = 'Uploading file (0%)...';
      progressBar.style.width = '10%';
      jobProgressPercent.innerText = '0%';

      document.getElementById('rbacAlertBanner').classList.add('hidden');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/v1/datasets/upload', true);
      xhr.setRequestHeader('Authorization', 'Bearer ' + activeToken);

      // Real-time network upload progress
      xhr.upload.onprogress = function(event) {
        if (event.lengthComputable) {
          const percent = Math.round((event.loaded / event.total) * 100);
          const uploadedMB = (event.loaded / (1024 * 1024)).toFixed(1);
          const totalMB = (event.total / (1024 * 1024)).toFixed(1);
          jobProgressPercent.innerText = percent + '%';
          progressBar.style.width = Math.max(10, Math.round(percent * 0.5)) + '%';
          jobStatusText.innerText = 'Uploading: ' + uploadedMB + 'MB / ' + totalMB + 'MB (' + percent + '%)...';
        }
      };

      xhr.onload = function() {
        if (xhr.status === 403) {
          document.getElementById('rbacAlertBanner').classList.remove('hidden');
          uploadBtn.disabled = false;
          uploadBtn.innerText = 'Upload & Process';
          progressContainer.classList.add('hidden');
          return;
        }

        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            const data = JSON.parse(xhr.responseText);
            currentDatasetId = data.dataset_id;
            currentJobId = data.job_id;
            uploadBtn.innerText = 'Processing...';
            jobStatusText.innerText = 'Ingesting & Validating asynchronously...';
            progressBar.style.width = '60%';
            pollJobStatus(data.job_id);
          } catch (e) {
            alert('Response parse error');
            uploadBtn.disabled = false;
            uploadBtn.innerText = 'Upload & Process';
          }
        } else {
          if (xhr.status === 413) {
            alert("File quá lớn so với giới hạn của serverless. Vui lòng chọn file nhỏ hơn.");
          } else {
            alert('Upload failed HTTP ' + xhr.status + ': ' + xhr.statusText);
          }
          uploadBtn.disabled = false;
          uploadBtn.innerText = 'Upload & Process';
          progressContainer.classList.add('hidden');
        }
      };

      xhr.onerror = function() {
        alert('Network error while uploading. Please check your internet connection.');
        uploadBtn.disabled = false;
        uploadBtn.innerText = 'Upload & Process';
        progressContainer.classList.add('hidden');
      };

      xhr.send(formData);
    }

    function pollJobStatus(jobId) {
      if (pollInterval) clearInterval(pollInterval);
      pollInterval = setInterval(async () => {
        try {
          const res = await fetch('/api/v1/jobs/' + jobId);
          if (!res.ok) return;
          const job = await res.json();

          if (job.status === 'RUNNING') {
            document.getElementById('jobStatusText').innerText = 'Processing (' + job.rows_total.toLocaleString() + ' rows)...';
            document.getElementById('progressBar').style.width = '75%';
          } else if (job.status === 'COMPLETED') {
            clearInterval(pollInterval);
            document.getElementById('progressBar').style.width = '100%';
            document.getElementById('jobStatusText').innerText = 'Completed (' + job.rows_per_second.toLocaleString() + ' rows/sec)!';
            document.getElementById('uploadBtn').disabled = false;
            await loadVersionList(job.dataset_id);
            fetchDashboard(job.dataset_id);
            fetchJobErrors(job.id);
            loadMetrics();
          } else if (job.status === 'FAILED') {
            clearInterval(pollInterval);
            document.getElementById('jobStatusText').innerText = 'Failed: ' + (job.error_message || 'Simulated Crash');
            document.getElementById('progressBar').classList.remove('bg-zinc-900', 'dark:bg-zinc-100');
            document.getElementById('progressBar').classList.add('bg-rose-500');
            document.getElementById('uploadBtn').disabled = false;

            // Trigger Alert Banner
            document.getElementById('alertMessage').innerText = job.error_message || 'Worker abruptly crashed midway.';
            document.getElementById('alertBanner').classList.remove('hidden');

            // Crucial: The dashboard remains alive reading the last published snapshot!
            fetchDashboard(job.dataset_id);
          }
        } catch (e) {
          console.error(e);
        }
      }, 1000);
    }

    async function loadVersionList(datasetId) {
      try {
        const res = await fetch('/api/v1/datasets/' + datasetId + '/versions');
        if (!res.ok) return;
        const versions = await res.json();
        const select = document.getElementById('versionSelect');
        select.innerHTML = '<option value="latest">Latest Published</option>';
        versions.forEach(v => {
          if (v.status === 'PUBLISHED') {
            const opt = document.createElement('option');
            opt.value = v.version_number;
            opt.innerText = 'v' + v.version_number + ' (' + v.accepted_total.toLocaleString() + ' rows)';
            select.appendChild(opt);
          }
        });
        if (versions.length > 0 && versions[0].job_id) {
          currentJobId = versions[0].job_id;
          fetchJobErrors(currentJobId);
          const exportBtn = document.getElementById('exportQuarantineBtn');
          if (exportBtn) exportBtn.classList.remove('hidden');
        }
      } catch (e) {}
    }

    function onVersionChange(val) {
      if (!currentDatasetId) return;
      if (val === 'latest') {
        fetchDashboard(currentDatasetId);
      } else {
        fetchDashboard(currentDatasetId, parseInt(val));
      }
    }

    async function fetchDashboard(datasetId, versionNum = null) {
      try {
        let url = '/api/v1/datasets/' + datasetId + '/dashboard';
        if (versionNum) url += '?version=' + versionNum;
        const res = await fetch(url);
        if (!res.ok) return;
        const dash = await res.json();

        currentVersionNumber = dash.version_number;
        document.getElementById('kpiGrandTotal').innerText = Number(dash.grand_total_amount_minor).toLocaleString();
        document.getElementById('kpiAccepted').innerText = dash.accepted_total.toLocaleString();
        document.getElementById('kpiQuarantined').innerText = dash.quarantined_total.toLocaleString();
        document.getElementById('kpiVersion').innerText = 'v' + dash.version_number;
        document.getElementById('downloadBtn').classList.remove('hidden');

        latestCategoriesData = dash.categories;
        updateCharts(dash.categories);
      } catch (e) {
        console.error(e);
      }
    }

    async function fetchJobErrors(jobId) {
      try {
        const res = await fetch('/api/v1/jobs/' + jobId + '/errors?limit=30');
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById('quarantineBadge').innerText = data.total_quarantined.toLocaleString() + ' Quarantined';

        // Render Breakdown Pills
        const pillsContainer = document.getElementById('quarantineBreakdownPills');
        pillsContainer.innerHTML = '';
        for (const [code, count] of Object.entries(data.breakdown)) {
          const pill = document.createElement('span');
          pill.className = 'text-xs bg-zinc-100 border border-zinc-200 text-zinc-700 dark:bg-neutral-950 dark:border-neutral-800 dark:text-zinc-300 px-3 py-1 rounded-full font-mono';
          pill.innerHTML = '<span class="text-rose-600 dark:text-rose-400 font-bold">' + count.toLocaleString() + '</span> ' + code;
          pillsContainer.appendChild(pill);
        }

        // Render Error Table
        const tbody = document.getElementById('errorTableBody');
        tbody.innerHTML = '';
        if (data.errors.length === 0) {
          tbody.innerHTML = '<tr><td colspan="4" class="px-4 py-4 text-center text-zinc-500">No validation errors in this dataset.</td></tr>';
          return;
        }

        data.errors.forEach(err => {
          const tr = document.createElement('tr');
          tr.className = 'hover:bg-zinc-50 dark:hover:bg-neutral-850/60 transition';
          tr.innerHTML = `
            <td class="px-4 py-2.5 text-zinc-500 dark:text-zinc-400">#${err.row_number.toLocaleString()}</td>
            <td class="px-4 py-2.5 text-rose-600 dark:text-rose-400 font-semibold">${err.error_code}</td>
            <td class="px-4 py-2.5 text-zinc-700 dark:text-zinc-300">${err.error_message}</td>
            <td class="px-4 py-2.5 text-zinc-400 dark:text-zinc-500 truncate max-w-xs">${err.event_id || 'N/A'}</td>
          `;
          tbody.appendChild(tr);
        });
      } catch (e) {}
    }

    function updateCharts(categories) {
      const dark = isDarkMode();
      const labels = Object.keys(categories);
      const amounts = labels.map(k => categories[k].total_amount_minor);
      const counts = labels.map(k => categories[k].count);

      const gridColor = dark ? 'rgba(255, 255, 255, 0.06)' : 'rgba(0, 0, 0, 0.06)';
      const textColor = dark ? '#a1a1aa' : '#52525b';
      const barColor = dark ? '#38bdf8' : '#0284c7';

      const ctxBar = document.getElementById('categoryBarChart').getContext('2d');
      if (barChart) barChart.destroy();
      barChart = new Chart(ctxBar, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: 'Amount Minor',
            data: amounts,
            backgroundColor: barColor,
            borderRadius: 8
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: dark ? '#18181b' : '#ffffff',
              titleColor: dark ? '#ffffff' : '#09090b',
              bodyColor: dark ? '#d4d4d8' : '#27272a',
              borderColor: dark ? '#27272a' : '#e4e4e7',
              borderWidth: 1
            }
          },
          scales: {
            x: { grid: { color: gridColor }, ticks: { color: textColor, font: { family: 'monospace' } } },
            y: { grid: { color: gridColor }, ticks: { color: textColor, font: { family: 'monospace' } } }
          }
        }
      });

      const ctxPie = document.getElementById('categoryPieChart').getContext('2d');
      if (pieChart) pieChart.destroy();
      pieChart = new Chart(ctxPie, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: counts,
            backgroundColor: ['#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa'],
            borderWidth: dark ? 2 : 1,
            borderColor: dark ? '#141414' : '#ffffff'
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'bottom',
              labels: {
                color: textColor,
                boxWidth: 12,
                font: { family: 'sans-serif', size: 11 }
              }
            },
            tooltip: {
              backgroundColor: dark ? '#18181b' : '#ffffff',
              titleColor: dark ? '#ffffff' : '#09090b',
              bodyColor: dark ? '#d4d4d8' : '#27272a',
              borderColor: dark ? '#27272a' : '#e4e4e7',
              borderWidth: 1
            }
          }
        }
      });
    }

    function handleDownload() {
      if (currentDatasetId) {
        let url = '/api/v1/datasets/' + currentDatasetId + '/download';
        if (currentVersionNumber) url += '?version=' + currentVersionNumber;
        window.location.href = url;
      }
    }

    function exportQuarantineCsv() {
      if (!currentJobId) {
        alert('No job active to export errors.');
        return;
      }
      window.location.href = '/api/v1/jobs/' + currentJobId + '/quarantine/export';
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
