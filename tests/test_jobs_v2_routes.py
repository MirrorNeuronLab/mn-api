from __future__ import annotations

import json

import pytest

from mn_api import state
from mn_api.routes import jobs_v2


class StableJobClient:
    def __init__(self):
        self.calls = []

    def create_stable_job(self, manifest_json, payloads, **kwargs):
        self.calls.append(("create_stable_job", manifest_json, payloads, kwargs))
        return json.dumps({"version": 2, "job_id": "job-1", "status": "active"})

    def list_stable_jobs(self, *, include_archived=False):
        self.calls.append(("list_stable_jobs", include_archived))
        return json.dumps({"version": 2, "data": []})

    def get_stable_job(self, job_id):
        self.calls.append(("get_stable_job", job_id))
        return json.dumps({"version": 2, "job_id": job_id, "status": "active"})

    def update_stable_job(self, job_id, attrs):
        self.calls.append(("update_stable_job", job_id, attrs))
        return json.dumps({"version": 2, "job_id": job_id, **attrs})

    def archive_stable_job(self, job_id):
        self.calls.append(("archive_stable_job", job_id))
        return json.dumps({"version": 2, "job_id": job_id, "status": "archived"})

    def reset_stable_job_data(self, job_id):
        self.calls.append(("reset_stable_job_data", job_id))
        return json.dumps({"version": 2, "job_id": job_id, "data_generation": 2})

    def delete_stable_job(self, job_id, *, confirmed=False):
        self.calls.append(("delete_stable_job", job_id, confirmed))
        return json.dumps({"version": 2, "job_id": job_id, "status": "deleted"})

    def start_run(self, job_id, **kwargs):
        self.calls.append(("start_run", job_id, kwargs))
        return json.dumps({"version": 2, "job_id": job_id, "run_id": "run-1", "status": "pending"})

    def list_runs(self, job_id):
        self.calls.append(("list_runs", job_id))
        return json.dumps({"version": 2, "job_id": job_id, "data": []})

    def get_run(self, run_id):
        self.calls.append(("get_run", run_id))
        return json.dumps({"version": 2, "job_id": "job-1", "run_id": run_id})

    def pause_run(self, run_id):
        self.calls.append(("pause_run", run_id))
        return json.dumps({"version": 2, "run_id": run_id, "status": "paused"})

    def resume_run(self, run_id):
        self.calls.append(("resume_run", run_id))
        return json.dumps({"version": 2, "run_id": run_id, "status": "running"})

    def cancel_run(self, run_id):
        self.calls.append(("cancel_run", run_id))
        return json.dumps({"version": 2, "run_id": run_id, "status": "cancelled"})

    def delete_run(self, run_id, *, confirmed=False):
        self.calls.append(("delete_run", run_id, confirmed))
        return json.dumps({"version": 2, "run_id": run_id, "status": "deleted"})

    def create_job_schedule(self, job_id, **kwargs):
        self.calls.append(("create_job_schedule", job_id, kwargs))
        return json.dumps({"version": 2, "job_id": job_id, "schedule_id": "schedule-1"})


class FailingStableJobClient:
    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise RuntimeError("private runtime failure")

        return fail


def test_v2_job_and_run_lifecycle_routes(monkeypatch, api_client):
    runtime = StableJobClient()
    monkeypatch.setattr(state, "client", runtime)

    create = api_client.post(
        "/api/v2/jobs",
        json={
            "manifest_json": '{"graph_id":"g","nodes":[]}',
            "job_id": "job-1",
            "resolved_configuration": {"mode": "safe"},
            "storage": {"rag": {"access": "write"}},
        },
    )
    assert create.status_code == 200
    assert create.json()["job_id"] == "job-1"

    assert api_client.get("/api/v2/jobs?include_archived=true").status_code == 200
    assert api_client.get("/api/v2/jobs/job-1").json()["status"] == "active"
    assert api_client.patch(
        "/api/v2/jobs/job-1", json={"attrs": {"job_name": "updated"}}
    ).json()["job_name"] == "updated"
    assert api_client.post("/api/v2/jobs/job-1/archive").json()["status"] == "archived"
    assert api_client.post("/api/v2/jobs/job-1/data:reset").json()["data_generation"] == 2

    started = api_client.post(
        "/api/v2/jobs/job-1/runs", json={"run_id": "run-1", "inputs": {"value": 1}}
    )
    assert started.json()["run_id"] == "run-1"
    assert api_client.get("/api/v2/jobs/job-1/runs").status_code == 200
    assert api_client.get("/api/v2/runs/run-1").json()["job_id"] == "job-1"

    for action in ("pause", "resume", "cancel"):
        assert api_client.post(f"/api/v2/runs/run-1/{action}").status_code == 200

    assert api_client.post(
        "/api/v2/jobs/job-1/schedules",
        json={"schedule": {"kind": "periodic", "every": "1h"}},
    ).json()["schedule_id"] == "schedule-1"
    assert api_client.request(
        "DELETE", "/api/v2/runs/run-1", json={"confirmed": True}
    ).json()["status"] == "deleted"
    assert api_client.request(
        "DELETE", "/api/v2/jobs/job-1", json={"confirmed": True}
    ).json()["status"] == "deleted"

    assert runtime.calls[0][0] == "create_stable_job"
    assert ("delete_run", "run-1", True) in runtime.calls
    assert ("delete_stable_job", "job-1", True) in runtime.calls


def test_v2_create_requires_manifest_or_bundle(monkeypatch, api_client):
    runtime = StableJobClient()
    monkeypatch.setattr(state, "client", runtime)

    response = api_client.post("/api/v2/jobs", json={})

    assert response.status_code == 422
    assert runtime.calls == []


def test_v2_create_accepts_uploaded_bundle_reference(monkeypatch, api_client):
    runtime = StableJobClient()
    monkeypatch.setattr(state, "client", runtime)
    monkeypatch.setattr(
        jobs_v2,
        "load_uploaded_bundle",
        lambda bundle_path, _upload_root: (
            '{"graph_id":"uploaded","nodes":[]}',
            {"worker.py": b"pass"},
        ),
    )

    response = api_client.post(
        "/api/v2/jobs",
        json={"_bundle_path": "bundle-token", "job_id": "uploaded-job"},
    )

    assert response.status_code == 200
    operation, manifest_json, payloads, kwargs = runtime.calls[0]
    assert operation == "create_stable_job"
    assert json.loads(manifest_json)["graph_id"] == "uploaded"
    assert payloads == {"worker.py": b"pass"}
    assert kwargs["job_id"] == "uploaded-job"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v2/jobs", {"manifest_json": '{"nodes":[]}'}),
        ("GET", "/api/v2/jobs", None),
        ("GET", "/api/v2/jobs/job-1", None),
        ("PATCH", "/api/v2/jobs/job-1", {"attrs": {"job_name": "updated"}}),
        ("POST", "/api/v2/jobs/job-1/archive", None),
        ("POST", "/api/v2/jobs/job-1/data:reset", None),
        ("DELETE", "/api/v2/jobs/job-1", {"confirmed": True}),
        ("POST", "/api/v2/jobs/job-1/runs", {"run_id": "run-1"}),
        ("GET", "/api/v2/jobs/job-1/runs", None),
        (
            "POST",
            "/api/v2/jobs/job-1/schedules",
            {"schedule": {"kind": "periodic", "every": "1h"}},
        ),
        ("GET", "/api/v2/runs/run-1", None),
        ("POST", "/api/v2/runs/run-1/pause", None),
        ("POST", "/api/v2/runs/run-1/resume", None),
        ("POST", "/api/v2/runs/run-1/cancel", None),
        ("DELETE", "/api/v2/runs/run-1", {"confirmed": True}),
    ],
)
def test_v2_routes_sanitize_runtime_failures(
    monkeypatch, api_client, method, path, body
):
    monkeypatch.setattr(state, "client", FailingStableJobClient())

    kwargs = {"json": body} if body is not None else {}
    response = api_client.request(method, path, **kwargs)

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"
    assert "private runtime failure" not in response.text


def test_v2_create_resolves_a_trusted_catalog_blueprint(monkeypatch, api_client):
    runtime = StableJobClient()
    monkeypatch.setattr(state, "client", runtime)
    monkeypatch.setattr(state, "refresh_config_from_env", lambda: object())
    monkeypatch.setattr(
        jobs_v2,
        "find_blueprint",
        lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}),
    )
    monkeypatch.setattr(
        jobs_v2, "create_blueprint_run_id", lambda _blueprint_id: "catalog-bootstrap-run"
    )

    def load_bundle(repo_root, blueprint, run_id, **kwargs):
        assert repo_root == "/catalog"
        assert blueprint == {"id": "researcher"}
        assert run_id == "catalog-bootstrap-run"
        assert kwargs["config_overrides"] == {"mode": "safe"}
        return '{"graph_id":"researcher","nodes":[]}', {"worker.py": b"pass"}

    monkeypatch.setattr(jobs_v2, "load_blueprint_bundle", load_bundle)

    response = api_client.post(
        "/api/v2/jobs",
        json={
            "blueprint_id": "researcher",
            "resolved_configuration": {"mode": "safe"},
        },
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-1"
    operation, manifest_json, payloads, kwargs = runtime.calls[0]
    assert operation == "create_stable_job"
    assert json.loads(manifest_json)["graph_id"] == "researcher"
    assert payloads["worker.py"] == b"pass"
    assert kwargs == {
        "job_id": "",
        "resolved_configuration": {"mode": "safe"},
        "storage": {},
    }
