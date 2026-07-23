from __future__ import annotations

import json
import os
from pathlib import Path

def test_run_export_and_compare_error_paths(api_client, run_writer):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    run_writer(runs_root, "run-a", final_artifact={"score": 1})
    run_writer(runs_root, "run-b", final_artifact={"score": 2})

    unsupported = api_client.get("/api/v1/runs/run-a/export?format=xml")
    missing_export = api_client.get("/api/v1/runs/missing/export")
    missing_compare = api_client.post("/api/v1/runs:compare", json={"run_a": "run-a", "run_b": "missing"})

    assert unsupported.status_code == 400
    assert unsupported.json()["detail"] == "unsupported export format"
    assert missing_export.status_code == 404
    assert missing_compare.status_code == 404


def test_run_human_routes_call_observability_tools(monkeypatch, api_client, run_writer):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    run_writer(runs_root, "run-human")
    calls = []
    monkeypatch.setattr(
        "mn_api.routes.runs._observability_tools",
        lambda: {
            "acknowledge_human_notice": lambda run_id, notice_id, payload, runs_root=None: calls.append(
                ("ack", run_id, notice_id, payload)
            )
            or {"acked": notice_id},
            "list_pending_human_requests": lambda run_id, runs_root=None: [{"request_id": "req-1"}],
            "read_human_events": lambda run_id, runs_root=None, status=None: [{"status": status or "all"}],
            "record_human_response": lambda run_id, request_id, payload, runs_root=None: calls.append(
                ("response", run_id, request_id, payload)
            )
            or {"recorded": request_id},
        },
    )

    pending = api_client.get("/api/v1/runs/run-human/human?status=pending")
    response = api_client.post("/api/v1/runs/run-human/human/req-1/response", json={"answer": "yes"})
    ack = api_client.post("/api/v1/runs/run-human/human/notice-1/ack", json={"seen": True})

    assert pending.json()["data"] == [{"request_id": "req-1"}]
    assert response.json() == {"version": 1, "recorded": "req-1"}
    assert ack.json() == {"version": 1, "acked": "notice-1"}
    assert ("response", "run-human", "req-1", {"answer": "yes"}) in calls
    assert ("ack", "run-human", "notice-1", {"seen": True}) in calls


def test_run_resource_duration_validation(api_client, run_writer):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    run_writer(runs_root, "run-resources")

    empty = api_client.get("/api/v1/runs/run-resources/resources?window=&bucket=1h")
    unsupported = api_client.get("/api/v1/runs/run-resources/resources?window=1w&bucket=1h")

    assert empty.status_code == 400
    assert empty.json()["detail"] == "duration cannot be empty"
    assert unsupported.status_code == 400
    assert unsupported.json()["detail"] == "unsupported duration unit: w"


def test_run_ui_video_serves_allowed_local_file(api_client, run_writer):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    run_dir = run_writer(runs_root, "run-video")
    video = run_dir / "clip.mp4"
    video.write_bytes(b"video")
    (run_dir / "ui.json").write_text(
        json.dumps({"components": [{"type": "video", "source": "clip.mp4"}], "metadata": {}}),
        encoding="utf-8",
    )

    response = api_client.get("/api/v1/runs/run-video/ui/video")

    assert response.status_code == 200
    assert response.content == b"video"


def test_run_ui_video_rejects_file_outside_allowed_roots(api_client, run_writer, tmp_path):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    run_dir = run_writer(runs_root, "run-forbidden-video")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"secret")
    (run_dir / "ui.json").write_text(
        json.dumps({"components": [{"type": "video", "source": str(outside)}], "metadata": {}}),
        encoding="utf-8",
    )

    response = api_client.get("/api/v1/runs/run-forbidden-video/ui/video")

    assert response.status_code == 403
    assert response.json()["detail"] == "video source is outside allowed roots"


def test_run_artifact_rejects_traversal_before_file_lookup(api_client, run_writer, tmp_path):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    run_writer(runs_root, "run-artifact")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    response = api_client.get("/api/v1/runs/run-artifact/artifacts/%2E%2E/secret.txt")

    assert response.status_code in {400, 404}
    if response.status_code == 400:
        assert response.json()["detail"] == "invalid artifact path"
