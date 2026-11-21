import concurrent.futures
import time
import statistics
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.database import SessionLocal, Base, engine
from backend.app.models import Dataset, AggregateVersion

@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c

def test_api_health_and_list(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"

def test_2_process_and_10_concurrent_readers(client):
    """
    Test 2 requirement from brief:
    Process 100000 rows while 10 users read the dashboard; record latency and show the API remains responsive.
    Reports p95 latency.
    """
    # 1. Start upload of 100k rows
    csv_file_path = "data/benchmark_100k.csv"
    with open(csv_file_path, "rb") as f:
        upload_res = client.post(
            "/api/v1/datasets/upload",
            files={"file": ("benchmark_100k.csv", f, "text/csv")},
            data={"name": "Concurrency Benchmark Test"}
        )
    assert upload_res.status_code == 202
    data = upload_res.json()
    job_id = data["job_id"]
    dataset_id = data["dataset_id"]

    # 2. Concurrently simulate 10 users querying dashboard / status repeatedly
    latencies_ms = []
    concurrency = 10
    total_requests_per_worker = 15

    def user_read_session(worker_id: int):
        worker_latencies = []
        for _ in range(total_requests_per_worker):
            t0 = time.perf_counter()
            # User reads dashboard or system metrics
            resp = client.get("/api/v1/metrics")
            t1 = time.perf_counter()
            assert resp.status_code == 200
            worker_latencies.append((t1 - t0) * 1000.0)
            time.sleep(0.01) # Small gap between user requests
        return worker_latencies

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(user_read_session, i) for i in range(concurrency)]
        for fut in concurrent.futures.as_completed(futures):
            latencies_ms.extend(fut.result())

    # Wait for job to finish
    max_wait = 20
    start_wait = time.time()
    job_finished = False
    while time.time() - start_wait < max_wait:
        status_res = client.get(f"/api/v1/jobs/{job_id}")
        assert status_res.status_code == 200
        job_data = status_res.json()
        if job_data["status"] == "COMPLETED":
            job_finished = True
            break
        elif job_data["status"] == "FAILED":
            pytest.fail(f"Job failed: {job_data.get('error_message')}")
        time.sleep(0.5)

    assert job_finished, "Ingestion job must complete within timeout"

    # Compute p95 latency
    latencies_ms.sort()
    p95_idx = int(len(latencies_ms) * 0.95)
    p95_latency = latencies_ms[p95_idx]
    median_latency = statistics.median(latencies_ms)

    print(f"\n[TEST 2 PASSED - CONCURRENCY & LATENCY BENCHMARK]")
    print(f"Total concurrent reader requests: {len(latencies_ms)} across {concurrency} users")
    print(f"Median Latency: {median_latency:.2f} ms")
    print(f"P95 Latency: {p95_latency:.2f} ms")
    assert p95_latency < 100.0, f"P95 latency should remain well under 100ms (got {p95_latency:.2f}ms)"
