from __future__ import annotations

import json
from types import SimpleNamespace

import grpc
import pytest
from mn_sdk.staged_artifacts import ArtifactIntegrityError, ArtifactNotReadyError, StagedArtifactError
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


def test_cancel_all_jobs_reports_runtime_failure(monkeypatch, api_client):
    class FailingClient:
        def list_jobs(self, _limit, _include_terminal):
            raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.post("/api/v1/jobs:cancel-all")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_cancel_all_jobs_cleans_up_when_one_cancellation_fails(monkeypatch, api_client):
    class FailingCancelClient:
        def __init__(self):
            self.calls = []

        def list_jobs(self, _limit, _include_terminal):
            self.calls.append("list_jobs")
            return json.dumps({"data": [{"job_id": "job-ok", "status": "running"}, {"job_id": "job-fail", "status": "running"}]})

        def cancel_job(self, job_id):
            self.calls.append(("cancel_job", job_id))
            if job_id == "job-fail":
                raise RuntimeError("cancel failed")

    cleaned = []
    client = FailingCancelClient()
    monkeypatch.setattr(state, "client", client)
    monkeypatch.setattr("mn_api.routes.jobs.cleanup_blueprint_processes_for_job", cleaned.append)

    response = api_client.post("/api/v1/jobs/cancel-all")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"
    assert cleaned == ["job-ok", "job-fail"]


def test_cleanup_jobs_reports_retry_failure_after_admin_token_error(monkeypatch, api_client):
    class PermissionDeniedRpcError(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.PERMISSION_DENIED

        def details(self):
            return "ClearJobs requires MN_GRPC_ADMIN_TOKEN"

    class FirstClient:
        def clear_jobs(self):
            raise PermissionDeniedRpcError()

    class RetryClient:
        def clear_jobs(self):
            raise RuntimeError("retry failed")

    first_client = FirstClient()
    retry_client = RetryClient()
    monkeypatch.setattr(state, "client", first_client)
    monkeypatch.setattr(state, "close_client", lambda: setattr(state, "client", retry_client))

    response = api_client.post("/api/v1/jobs:cleanup")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_cleanup_jobs_reports_non_admin_runtime_failure(monkeypatch, api_client):
    class FailingClient:
        def clear_jobs(self):
            raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.post("/api/v1/jobs:cleanup")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_cleanup_jobs_does_not_retry_unrelated_rpc_error(monkeypatch, api_client):
    class UnrelatedRpcError(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.INTERNAL

        def details(self):
            return "permission denied"

    class FailingClient:
        def clear_jobs(self):
            raise UnrelatedRpcError()

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.post("/api/v1/jobs:cleanup")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_unfinished_jobs_reports_runtime_failure(monkeypatch, api_client):
    class FailingClient:
        def list_jobs(self, _limit, _include_terminal):
            raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.get("/api/v1/jobs/unfinished")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_list_jobs_reports_runtime_failure(monkeypatch, api_client):
    class FailingClient:
        def list_jobs(self, _limit, _include_terminal):
            raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.get("/api/v1/jobs")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_get_job_rejects_unknown_include_value(monkeypatch, api_client):
    monkeypatch.setattr(state, "client", SimpleNamespace())

    response = api_client.get("/api/v1/jobs/job-1?include=details")

    assert response.status_code == 400
    assert response.json()["detail"] == "include must be 'compact', 'summary', or 'full'"


def test_get_job_reports_runtime_failure(monkeypatch, api_client):
    monkeypatch.setattr(state, "client", SimpleNamespace(get_job=lambda _job_id: (_ for _ in ()).throw(RuntimeError("runtime unavailable"))))

    response = api_client.get("/api/v1/jobs/job-1?include=full")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_get_job_snapshot_rejects_unknown_kind(api_client):
    response = api_client.get("/api/v1/jobs/job-1/snapshots/unknown")

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown job snapshot"


def test_get_job_snapshot_reports_unavailable_reference(monkeypatch, api_client):
    monkeypatch.setattr(state, "client", SimpleNamespace(get_job=lambda _job_id: json.dumps({"job": {}})))

    response = api_client.get("/api/v1/jobs/job-1/snapshots/result")

    assert response.status_code == 404
    assert response.json()["detail"] == "result snapshot is unavailable"


def test_get_job_snapshot_reports_runtime_failure(monkeypatch, api_client):
    monkeypatch.setattr(
        state,
        "client",
        SimpleNamespace(get_job=lambda _job_id: (_ for _ in ()).throw(RuntimeError("runtime unavailable"))),
    )

    response = api_client.get("/api/v1/jobs/job-1/snapshots/result")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


@pytest.mark.parametrize(
    ("error", "status_code", "error_code"),
    [
        (ArtifactNotReadyError("artifact pending"), 503, "artifact_not_ready"),
        (ArtifactIntegrityError("artifact mismatch"), 500, "artifact_integrity_error"),
        (StagedArtifactError("artifact invalid"), 500, "artifact_resolution_error"),
    ],
)
def test_get_job_snapshot_maps_staged_artifact_errors(monkeypatch, api_client, error, status_code, error_code):
    reference = {
        "version": "mn.staged_artifact/v1",
        "storage": "syncthing",
        "submission_id": "submission-1",
        "relative_path": "outputs/runs/run-1/result.json",
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    monkeypatch.setattr(state, "client", SimpleNamespace(get_job=lambda _job_id: json.dumps({"job": {"result_ref": reference}})))
    monkeypatch.setattr("mn_api.routes.jobs.resolve_json_reference", lambda _reference: (_ for _ in ()).throw(error))

    response = api_client.get("/api/v1/jobs/job-1/snapshots/result")

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == error_code
    if status_code == 503:
        assert response.headers["retry-after"] == "1"


def test_dead_letters_reports_runtime_failure(monkeypatch, api_client):
    class FailingClient:
        def stream_events(self, *_args, **_kwargs):
            raise RuntimeError("stream unavailable")

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.get("/api/v1/jobs/job-1/dead-letters")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


def test_pause_job_uses_generic_problem_when_details_lookup_fails(monkeypatch, api_client):
    class BrokenDetailsError(Exception):
        def details(self):
            raise RuntimeError("details unavailable")

    class FailingClient:
        def pause_job(self, _job_id):
            raise BrokenDetailsError("pause failed")

    monkeypatch.setattr(state, "client", FailingClient())

    response = api_client.post("/api/v1/jobs/job-1/pause")

    assert response.status_code == 500
    assert response.json()["error"] == "MN_EXECUTION_FAILED"


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
