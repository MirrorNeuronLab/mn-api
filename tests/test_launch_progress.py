from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from mn_api import launch_progress, state
from mn_api.routes import blueprints as legacy
from mn_api.routes.v1 import blueprints, jobs
from tests.test_v1_contract import _client


def test_activity_heartbeat_ends_on_cancellation(monkeypatch):
    monkeypatch.setattr(launch_progress, "HEARTBEAT_SECONDS", 0.01)
    observed = Event()
    events = []

    def report(*event):
        events.append(event)
        if "elapsed" in event[1]:
            observed.set()

    with pytest.raises(KeyboardInterrupt):
        with launch_progress.launch_activity(report, "Prepare Docker", "Waiting for worker readiness."):
            assert observed.wait(2)
            raise KeyboardInterrupt
    count = len(events)
    observed.clear()
    assert not observed.wait(0.03)
    assert len(events) == count
    assert all("%" not in detail for _, detail, _ in events)


def test_progress_sink_failure_does_not_change_operation(monkeypatch):
    def broken(*_args):
        raise OSError("progress disk unavailable")

    with launch_progress.launch_activity(broken, "Prepare Docker", "Checking images"):
        result = 42
    assert result == 42
    with launch_progress.launch_activity(None, "Prepare Docker", "Checking images"):
        pass


def test_job_progress_can_be_read_while_submission_is_blocked(monkeypatch, tmp_path):
    client, runtime = _client(monkeypatch)
    monkeypatch.setenv("MN_LAUNCH_PROGRESS_DIR", str(tmp_path))
    monkeypatch.setattr(launch_progress, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(state, "refresh_config_from_env", lambda: state.config)
    monkeypatch.setattr(jobs, "find_blueprint", lambda *_args: (tmp_path, {"id": "sample"}))
    entered, release = Event(), Event()

    def load(*_args, progress_callback, **_kwargs):
        with launch_progress.launch_activity(
            progress_callback, "Preparing DockerWorker runtime.", "Waiting for the image and container."
        ):
            entered.set()
            assert release.wait(3)
        return json.dumps({"agents": {"nodes": []}}), {}

    monkeypatch.setattr(jobs, "load_blueprint_bundle", load)
    headers = {"X-Launch-Progress-ID": "launch-visible", "Idempotency-Key": "launch-visible"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/v1/jobs", headers=headers, json={"blueprint_id": "sample"})
        try:
            assert entered.wait(2)
            snapshot = client.get("/api/v1/launch-progress/launch-visible").json()
            assert snapshot["status"] == "running"
            assert "DockerWorker" in snapshot["latest"]["message"]
            assert not snapshot["completed"]
            assert not future.done()
        finally:
            release.set()
        response = future.result(timeout=3)
    assert response.status_code == 201
    assert client.get("/api/v1/launch-progress/launch-visible").json()["completed"]
    replay = client.post("/api/v1/jobs", headers=headers, json={"blueprint_id": "sample"})
    assert replay.json() == response.json()
    assert sum(call[0] == "create_job" for call in runtime.calls) == 1


def test_progress_endpoint_auth_validation_and_sanitization(monkeypatch, tmp_path):
    client, _runtime = _client(monkeypatch)
    monkeypatch.setenv("MN_LAUNCH_PROGRESS_DIR", str(tmp_path))
    state.config.api_token = "test-token"
    assert client.get("/api/v1/launch-progress/sample").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert client.get("/api/v1/launch-progress/bad!", headers=headers).status_code == 400
    assert client.get("/api/v1/launch-progress/unknown", headers=headers).json()["status"] == "pending"
    legacy.record_launch_progress(
        "sample", "launch", "failed", "secret-build-output", {"secret_environment": "private"}
    )
    response = client.get("/api/v1/launch-progress/sample", headers=headers)
    assert response.status_code == 200
    assert "secret-build-output" not in response.text
    assert "private" not in response.text


def test_submission_failure_preserves_exception_and_sanitizes_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("MN_LAUNCH_PROGRESS_DIR", str(tmp_path))
    failure = RuntimeError("private runtime details")
    with pytest.raises(RuntimeError) as caught, launch_progress.observe_submission("failed-request"):
        raise failure
    assert caught.value is failure
    snapshot = launch_progress.public_progress_snapshot("failed-request")
    assert snapshot["completed"]
    assert snapshot["status"] == "failed"
    assert "private runtime details" not in json.dumps(snapshot)


def test_blueprint_run_forwards_progress_id_without_changing_request(monkeypatch, tmp_path):
    client, _runtime = _client(monkeypatch)
    monkeypatch.setattr(blueprints, "find_blueprint", lambda *_args: (tmp_path, {"id": "sample"}))
    monkeypatch.setattr(blueprints, "_config", lambda: state.config)
    seen = []

    def run(_root, _blueprint, request):
        seen.append(request)
        return {"run_id": "run-1", "job_id": "job-1"}

    monkeypatch.setattr(legacy, "run_blueprint_record", run)
    response = client.post("/api/v1/blueprints/sample/runs", headers={"X-Launch-Progress-ID": "sample-run"}, json={})
    assert response.status_code == 202
    assert seen[0].progress_id == "sample-run"
    assert response.json()["run_id"] == "run-1"
