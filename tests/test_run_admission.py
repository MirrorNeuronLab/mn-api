import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]))

    class Runtime:
        def __init__(self):
            self.calls = []

        def get_job(self, job_id):
            return json.dumps({"job_id": job_id, "resolved_configuration": {"size": 1}})

        def check_run_admission(self, job_id, **kwargs):
            self.calls.append((job_id, kwargs))
            return json.dumps({"status": "runnable_later", "node_label": "Spark", "blockers": [
                {"code": "MN_GPU_MEMORY_UNAVAILABLE", "resource": "gpu_memory_free_mb", "available": 12, "required": 20, "unit": "GiB"}
            ]})

    runtime = Runtime()
    monkeypatch.setattr(state, "client", runtime)
    return TestClient(create_app()), runtime


def test_checks_without_creating_a_run_or_preparing_native_resources(client):
    api, runtime = client
    result = api.post("/api/v1/jobs/job-1/run-admission-checks", json={})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "runnable_later"
    assert runtime.calls == [("job-1", {"run_id": ""})]


def test_resume_is_bound_to_the_existing_run(client):
    api, runtime = client
    result = api.post("/api/v1/jobs/job-1/run-admission-checks", json={"action": "resume", "run_id": "run-1"})
    assert result.status_code == 200
    assert runtime.calls == [("job-1", {"run_id": "run-1"})]


@pytest.mark.parametrize("body,status", [({"action": "resume"}, 422), ({"action": "execute"}, 422),
    ({"config_overrides": {"size": 2}}, 409), ({"unexpected": True}, 422)])
def test_rejects_invalid_or_unsaved_launch_settings_without_side_effects(client, body, status):
    api, runtime = client
    result = api.post("/api/v1/jobs/job-1/run-admission-checks", json=body)
    assert result.status_code == status, result.text
    assert runtime.calls == []
