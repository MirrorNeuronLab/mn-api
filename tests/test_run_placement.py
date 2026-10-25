from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.routes.v1 import jobs


@pytest.fixture
def placement_client(monkeypatch):
    run = {"run_id": "run-1", "status": "running", "placement": {
        "owner_node": "spark@pc", "node_label": "Spark", "reservation": {
            "state": "reserved", "mode": "shared", "cpu_cores": 2.5,
            "memory_mb": 8192, "disk_mb": 1024, "gpu_count": 1, "gpu_memory_mb": 4096,
        },
    }}
    reads = []
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]))
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(get_run=lambda run_id: reads.append(run_id) or run.copy()))
    monkeypatch.setattr(jobs, "_runtime_output_id", lambda *args, **kwargs: "run-1")
    monkeypatch.setattr(jobs.runtime_job_routes, "_compact_job_detail", lambda _: {
        "status": "unknown", "placement": {"owner_node": "stale"}, "job": {}, "summary": {},
    })
    return TestClient(create_app()), run, reads


@pytest.mark.parametrize("endpoint", ["", "/monitor"])
def test_run_info_and_monitor_expose_the_same_current_reservation(placement_client, endpoint):
    client, run, reads = placement_client
    response = client.get(f"/api/v1/runs/run-1{endpoint}")
    assert response.status_code == 200
    assert response.json()["placement"] == run["placement"]
    assert reads == ["run-1"]
    run["status"] = "paused"
    assert client.get(f"/api/v1/runs/run-1{endpoint}").json()["placement"] == run["placement"]
    run["status"] = "completed"
    run["placement"]["reservation"] = {"state": "unreserved"}
    released = client.get(f"/api/v1/runs/run-1{endpoint}").json()["placement"]["reservation"]
    assert released["state"] == "unreserved"
    assert released.get("cpu_cores") is None


@pytest.mark.parametrize("endpoint", ["", "/monitor"])
def test_stored_history_cannot_claim_a_current_reservation(placement_client, monkeypatch, endpoint):
    client, run, _ = placement_client
    def unavailable(_):
        raise RuntimeError("offline")
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(get_run=unavailable))
    monkeypatch.setattr(jobs, "stored_run", lambda _: run.copy())
    monkeypatch.setattr(jobs, "stored_progress", lambda _: {})
    response = client.get(f"/api/v1/runs/run-1{endpoint}")
    assert response.status_code == 200
    assert response.json()["placement"] is None
    if endpoint == "/monitor":
        assert response.json()["job"]["placement"] is None


def test_monitor_does_not_replace_missing_core_placement_with_replica_data(placement_client):
    client, run, _ = placement_client
    del run["placement"]
    assert client.get("/api/v1/runs/run-1/monitor").json()["placement"] is None
