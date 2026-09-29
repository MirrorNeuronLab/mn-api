import json
from types import SimpleNamespace
import grpc
from fastapi.testclient import TestClient
from mn_api import state
from mn_api.app import create_app
from mn_api.routes.v1 import jobs

class Client:
    def get_job(self, job_id, **kwargs):
        return json.dumps({"job_id": job_id, "run_count": 0})
    def list_runs(self, job_id, **kwargs):
        return json.dumps({"items": []})

def app_client(monkeypatch, token=""):
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token=token, request_size_limit_bytes=1024*1024, cors_allow_origins=[]))
    monkeypatch.setattr(state, "client", Client())
    return TestClient(create_app())

def test_analysis_contract_and_auth(monkeypatch):
    client = app_client(monkeypatch, "secret")
    assert client.get("/api/v1/jobs/job-a/analysis").status_code == 401
    response = client.get("/api/v1/jobs/job-a/analysis", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["job_id"] == "job-a"
    assert value["runs"]["success_rate"] is None
    assert value["tokens"]["total_tokens"] == 0
    assert value["history_complete"] is True

def test_analysis_timeout(monkeypatch):
    client = app_client(monkeypatch)
    def fail(*args, **kwargs):
        raise TimeoutError("private diagnostics")
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(analyze_job=fail))
    response = client.get("/api/v1/jobs/job-a/analysis")
    assert response.status_code == 504
    assert "private diagnostics" not in response.text

def test_analysis_not_found(monkeypatch):
    client = app_client(monkeypatch)
    class Missing(grpc.RpcError):
        def code(self): return grpc.StatusCode.NOT_FOUND
        def details(self): return "Job not found"
    def fail(*args, **kwargs): raise Missing()
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(analyze_job=fail))
    assert client.get("/api/v1/jobs/missing/analysis").status_code == 404
