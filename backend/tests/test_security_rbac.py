import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c

def test_unauthorized_direct_api_call_rejected(client):
    """
    Project requirement:
    Test authorized and forbidden API calls directly, not only hidden browser buttons.
    Invalid token must receive 401 Unauthorized.
    """
    response = client.post(
        "/api/v1/datasets/upload",
        headers={"Authorization": "Bearer invalid-tampered-token-9999"},
        files={"file": ("test.csv", b"event_id,category,timestamp_utc,amount_minor\n", "text/csv")}
    )
    assert response.status_code == 401
    assert "Unauthorized" in response.json()["detail"]
    print("\n[RBAC TEST 1 PASSED] 401 Unauthorized on invalid direct API credentials.")

def test_analyst_forbidden_from_upload(client):
    """
    Project requirement:
    Analyst role must receive 403 Forbidden when attempting an upload/ingestion operation.
    """
    response = client.post(
        "/api/v1/datasets/upload",
        headers={"Authorization": "Bearer analyst-token-2026"},
        files={"file": ("test.csv", b"event_id,category,timestamp_utc,amount_minor\n", "text/csv")}
    )
    assert response.status_code == 403
    assert "Forbidden" in response.json()["detail"]
    assert "DATASET_OWNER" in response.json()["detail"]
    print("[RBAC TEST 2 PASSED] 403 Forbidden on direct upload call by Analyst role.")

def test_dataset_owner_authorized_to_upload(client):
    """
    Project requirement:
    DATASET_OWNER role is authorized and receives 202 Accepted.
    """
    response = client.post(
        "/api/v1/datasets/upload",
        headers={"Authorization": "Bearer owner-token-2026"},
        files={"file": ("sample_auth_test.csv", b"event_id,category,timestamp_utc,amount_minor\n1,PAYMENT,2026-01-01T00:00:00Z,1000\n", "text/csv")},
        data={"name": "RBAC Verification Dataset"}
    )
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "PENDING"
    assert "job_id" in data
    print("[RBAC TEST 3 PASSED] 202 Accepted on direct upload call by Dataset Owner.")
