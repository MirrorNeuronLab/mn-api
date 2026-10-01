import json
from types import SimpleNamespace

import grpc
import pytest
from fastapi.testclient import TestClient
from mn_api import state
from mn_api.app import create_app
from mn_api.routes.v1 import jobs

REVISION = "a" * 64


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]))
    class Runtime:
        calls = []
        def plan_run_retry(self, run_id, **kwargs):
            self.calls.append(("plan", run_id, kwargs))
            return json.dumps({"run_id": run_id, "eligible": True, "expected_attempt": 1, "checkpoint_revision": REVISION})
        def retry_run(self, run_id, **kwargs):
            self.calls.append(("retry", run_id, kwargs))
            return json.dumps({"run_id": run_id, "attempt": 2, "attempt_id": run_id + ":2", "status": "accepted"})
    runtime = Runtime()
    monkeypatch.setattr(state, "client", runtime)
    return TestClient(create_app()), runtime


def test_planning_and_submission_forward_explicit_selection(client):
    api, runtime = client
    assert api.post("/api/v1/runs/run-1/retry-plans", json={"configuration_overrides": {"budget.minutes": 60}}).json()["eligible"]
    body = {"configuration_overrides": {"budget.minutes": 60}, "expected_attempt": 1, "checkpoint_revision": REVISION}
    response = api.post("/api/v1/runs/run-1/retries", json=body, headers={"Idempotency-Key": "key-1"})
    assert response.status_code == 202, response.text
    assert runtime.calls[-1] == ("retry", "run-1", {**body, "idempotency_key": "key-1"})
    assert response.json()["attempt"] == 2


@pytest.mark.parametrize("body", [{"configuration_overrides": {"budget.minutes": True}},
    {"configuration_overrides": {"budget.minutes": []}}, {"unexpected": 1}])
def test_invalid_retry_settings_do_not_reach_core(client, body):
    api, runtime = client
    assert api.post("/api/v1/runs/run-1/retry-plans", json=body).status_code == 422
    assert runtime.calls == []


def test_submission_requires_key_and_revision(client):
    api, runtime = client
    assert api.post("/api/v1/runs/run-1/retries", json={}).status_code == 422
    assert runtime.calls == []


class RuntimeErrorResponse(grpc.RpcError):
    def __init__(self, status):
        self.status = status
    def code(self):
        return self.status
    def details(self):
        return "Core unavailable" if self.status == grpc.StatusCode.UNAVAILABLE else "Run missing"


@pytest.mark.parametrize("status,control", [(grpc.StatusCode.NOT_FOUND, "missing"), (grpc.StatusCode.UNAVAILABLE, "unavailable")])
def test_inspection_distinguishes_history_from_runtime_control(client, monkeypatch, status, control):
    api, runtime = client
    def fail(*args, **kwargs):
        raise RuntimeErrorResponse(status)
    monkeypatch.setattr(runtime, "get_run", fail, raising=False)
    monkeypatch.setattr(jobs, "stored_run", lambda _: {"run_id": "run-1", "record_source": "history", "status": "failed"})
    response = api.get("/api/v1/runs/run-1")
    assert response.status_code == 200
    assert response.json()["control_status"] == control
    assert response.json()["record_source"] == "history"
    assert response.json()["retry"]["available"] is False


@pytest.mark.parametrize("status,expected", [(grpc.StatusCode.NOT_FOUND, 200), (grpc.StatusCode.UNAVAILABLE, 503)])
def test_history_does_not_mask_runtime_unavailability(client, monkeypatch, status, expected):
    api, runtime = client
    def fail(*args, **kwargs):
        raise RuntimeErrorResponse(status)
    monkeypatch.setattr(runtime, "plan_run_retry", fail)
    monkeypatch.setattr(jobs, "stored_run", lambda _: {"run_id": "run-1", "status": "failed"})
    response = api.post("/api/v1/runs/run-1/retry-plans", json={})
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["eligible"] is False
        assert "control record is missing" in response.json()["reason"]
