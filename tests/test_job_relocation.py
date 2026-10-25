from types import SimpleNamespace

from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.routes.v1 import jobs


def test_relocation_uses_a_durable_idempotent_operation(monkeypatch):
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]))
    calls = []
    monkeypatch.setattr(jobs, "start_local_operation", lambda kind, options, work, **kwargs:
        calls.append((kind, options, work, kwargs)) or {"operation_id": "move-1", "status": "pending"})
    api = TestClient(create_app())
    body = {"target_node": "spark@pc", "transfer_id": "a" * 32}
    result = api.post("/api/v1/jobs/job-1/relocations", json=body)
    assert result.status_code == 202
    assert result.headers["location"].endswith("/operations/" + calls[0][3]["operation_id"])
    assert calls[0][0] == "relocate_job"
    assert calls[0][1] == {"job_id": "job-1", **body}
    assert calls[0][3]["durable"] is True
    assert api.post("/api/v1/jobs/job-1/relocations", json={**body, "transfer_id": "../bad"}).status_code == 422
    assert len(calls) == 1
