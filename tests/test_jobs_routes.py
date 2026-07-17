from __future__ import annotations

import json
from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

from mn_api import state
from mn_api.routes.jobs import _decode_event_payload


def test_unfinished_jobs_ignores_malformed_job_lists(monkeypatch, api_client, fake_runtime_client):
    fake_runtime_client.jobs_payload = {"data": "not-a-list"}
    monkeypatch.setattr(state, "client", fake_runtime_client)

    response = api_client.get("/api/v1/jobs/unfinished")

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert fake_runtime_client.calls[-1] == ("list_jobs", 500, False)


def test_cleanup_job_aliases_share_runtime_behavior(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)

    colon = api_client.post("/api/v1/jobs:cleanup")
    path = api_client.post("/api/v1/jobs/cleanup")

    assert colon.status_code == 200
    assert path.status_code == 200
    assert [call for call in fake_runtime_client.calls if call == ("clear_jobs",)] == [("clear_jobs",), ("clear_jobs",)]


def test_cancel_all_job_alias_cancels_cli_active_statuses(monkeypatch, api_client, fake_runtime_client):
    fake_runtime_client.jobs_payload = {
        "data": [
            {"job_id": "job-pending", "status": "pending"},
            {"job_id": "job-validated", "status": "validated"},
            {"job_id": "job-scheduled", "status": "scheduled"},
            {"job_id": "job-running", "status": "running"},
            {"job_id": "job-paused", "status": "paused"},
            {"job_id": "job-done", "status": "completed"},
        ]
    }
    cleaned = []
    monkeypatch.setattr(state, "client", fake_runtime_client)
    monkeypatch.setattr("mn_api.routes.jobs.cleanup_blueprint_processes_for_job", cleaned.append)

    response = api_client.post("/api/v1/jobs:cancel-all")

    active_ids = [record["job_id"] for record in fake_runtime_client.jobs_payload["data"][:-1]]
    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "status": "cancelled",
        "active_count": 5,
        "cancelled_count": 5,
        "cancelled_job_ids": active_ids,
    }
    assert fake_runtime_client.calls[0] == ("list_jobs", 2_147_483_647, False)
    assert [call[1] for call in fake_runtime_client.calls if call[0] == "cancel_job"] == active_ids
    assert cleaned == active_ids


def test_cancel_all_jobs_reports_no_active_jobs(monkeypatch, api_client, fake_runtime_client):
    fake_runtime_client.jobs_payload = {"data": [{"job_id": "job-done", "status": "completed"}]}
    monkeypatch.setattr(state, "client", fake_runtime_client)

    response = api_client.post("/api/v1/jobs/cancel-all")

    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "status": "no_active_jobs",
        "active_count": 0,
        "cancelled_count": 0,
        "cancelled_job_ids": [],
    }


def test_restore_job_rejects_invalid_base64_before_sdk_call(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)

    response = api_client.post(
        "/api/v1/jobs/restore",
        json={"backup_json": "{}", "bundle_files": {"manifest.json": "abc"}, "blueprint_id": "bp"},
    )

    assert response.status_code >= 400
    assert not any(call[0] == "restore_job_backup" for call in fake_runtime_client.calls)


def test_pause_resume_legacy_error_shape_is_narrowly_preserved(monkeypatch, api_client):
    class DetailError(Exception):
        def __init__(self, detail):
            self.detail = detail

        def details(self):
            return self.detail

    fake = SimpleNamespace(
        pause_job=lambda job_id: (_ for _ in ()).throw(DetailError(f"job {job_id} cannot be paused")),
        resume_job=lambda job_id: (_ for _ in ()).throw(DetailError(f"job {job_id} cannot be resumed")),
    )
    monkeypatch.setattr(state, "client", fake)

    pause = api_client.post("/api/v1/jobs/job-1/pause")
    resume = api_client.post("/api/v1/jobs/job-1/resume")

    assert pause.status_code == 500
    assert pause.json() == {"version": 1, "error": "job job-1 cannot be paused"}
    assert resume.status_code == 500
    assert resume.json() == {"version": 1, "error": "job job-1 cannot be resumed"}


def test_pause_resume_non_detail_errors_use_problem_contract(monkeypatch, api_client):
    fake = SimpleNamespace(pause_job=lambda job_id: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(state, "client", fake)

    response = api_client.post("/api/v1/jobs/job-1/pause")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_workflow_progress_websocket_reports_stream_errors(monkeypatch, api_client):
    class FakeClient:
        def stream_events(self, *_args, **_kwargs):
            raise RuntimeError("stream failed")

    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(
        "mn_api.routes.jobs._workflow_progress_snapshot_for_job",
        lambda job_id: {"job_id": job_id, "status": "running"},
    )

    with api_client.websocket_connect("/api/v1/jobs/job-1/workflow-progress/ws") as websocket:
        assert websocket.receive_json() == {"event": "snapshot", "data": {"job_id": "job-1", "status": "running"}}
        error = websocket.receive_json()
        assert error == {"event": "error", "data": {"job_id": "job-1", "error": "stream failed"}}
        try:
            websocket.receive_json()
        except WebSocketDisconnect:
            pass


def test_decode_event_payload_handles_unparseable_and_scalar_payloads():
    assert _decode_event_payload("{bad") == {"type": "unparseable_event", "message": "{bad"}
    assert _decode_event_payload(json.dumps(["not", "object"])) == {"type": "event", "payload": ["not", "object"]}
    assert _decode_event_payload(None) == {}
