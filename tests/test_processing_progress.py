import json

from mn_api.routes.v1 import jobs
from mn_sdk.workflow_progress import workflow_progress_snapshot
from tests.test_v1_contract import _client


def test_measured_speed_survives_rest_and_resumed_sse_without_private_payloads(monkeypatch):
    client, runtime = _client(monkeypatch)
    runtime.run_status = "running"
    scope = {"step_id": "read", "agent_id": "reader", "attempt_id": "attempt"}
    snapshot = workflow_progress_snapshot(
        {"workflow": {"steps": [{"id": "read"}]}},
        [
            {"type": "job_running"},
            {"type": "workflow_step_attempt_started", "payload": scope},
            {
                "type": "processing_progress",
                "payload": {
                    **scope,
                    "activity_kind": "diagnostic",
                    "processing": {
                        "schema": "mn.processing.v1",
                        "id": "scope",
                        "unit": "audio_second",
                        "status": "running",
                        "sequence": 2,
                        "completed": 150,
                        "total": 600,
                        "elapsed_seconds": 12,
                        "sample_at": 100,
                        "message": "PRIVATE TRANSCRIPT",
                    },
                },
            },
        ],
    )
    terminal = {**snapshot, "status": "completed", "steps": [], "current_step": None, "processing": []}
    snapshots = iter([snapshot, snapshot, terminal])

    def progress_for_run(_):
        progress = next(snapshots)
        runtime.run_status = progress["status"]
        return progress

    monkeypatch.setattr(jobs.runtime_job_routes, "_workflow_progress_snapshot_for_run", progress_for_run)
    monkeypatch.setattr(jobs.time, "sleep", lambda _: None)
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", lambda *a, **kw: {"data": []})
    response = client.get("/api/v1/runs/run-1/workflow-progress")
    assert response.status_code == 200
    receipt = response.json()["steps"][0]["processing"][0]
    assert receipt["completed"] / receipt["elapsed_seconds"] == 12.5
    assert response.json()["current_step"]["processing"] == response.json()["processing"] == [receipt]
    assert "PRIVATE" not in response.text
    stream = client.get("/api/v1/runs/run-1/events/stream", headers={"Last-Event-ID": "40"})
    assert stream.status_code == 200 and "id: 41" in stream.text
    updates = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
    snapshots = [update["data"] for update in updates if update["type"] == "run.snapshot"]
    assert snapshots[0]["steps"][0]["processing"] == [receipt]
    assert snapshots[-1]["processing"] == []
