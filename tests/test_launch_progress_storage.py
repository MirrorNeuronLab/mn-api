"""Characterize the retained progress API and its on-disk JSONL contract."""

import json

import pytest
from fastapi import HTTPException

from mn_api import blueprint_launch as progress


def test_progress_history_keeps_terminal_status_and_latest_phase(monkeypatch, tmp_path):
    monkeypatch.setenv("MN_LAUNCH_PROGRESS_DIR", str(tmp_path))
    progress.record_launch_progress(
        "run", "submit", "running", "Submitting", {"run_id": "run-1"},
        label="Submit", detail="Waiting", expectation="Ready", severity="info",
    )
    path = tmp_path / "run.jsonl"
    raw = json.loads(path.read_text())
    assert set(raw) == {
        "version", "ts", "phase", "status", "message", "details", "label",
        "detail", "expectation", "severity",
    }
    assert raw["version"] == 2
    assert raw["details"] == {"run_id": "run-1"}
    assert path.read_bytes().endswith(b"\n")
    with path.open("a") as stream:
        stream.write('\ninvalid\n[]\n"not an event"\n')
    progress.record_launch_progress("run", "launch", "completed", "Complete", {"job_id": 7})
    progress.record_launch_progress("run", "cleanup", "running", "Cleaning")

    snapshot = progress.launch_progress_snapshot(" run ")
    assert snapshot["schema_version"] == "mn.launch_progress.v2"
    assert snapshot["progress_id"] == "run"
    assert snapshot["run_id"] == "run-1"
    assert snapshot["job_id"] == "7"
    assert snapshot["completed"] is True
    assert snapshot["status"] == "completed"
    assert snapshot["current_phase"] == "cleanup"
    assert [event["phase"] for event in snapshot["events"]] == ["submit", "launch", "cleanup"]
    assert [phase["id"] for phase in snapshot["phases"]] == ["submit", "launch", "cleanup"]
    assert snapshot["phases"][0]["label"] == "Submit"


def test_progress_phase_replacement_preserves_first_seen_order():
    phases = progress.summarize_launch_progress_phases([
        {},
        {"step": "custom_step", "state": "pending", "detail": "First", "timestamp": "a"},
        {"id": "validation", "message": "Checking"},
        {"phase": "custom_step", "status": "completed", "message": "Last", "ts": "b"},
    ])
    assert phases == [
        {"version": 2, "id": "custom_step", "label": "Custom Step", "status": "completed",
         "message": "Last", "updated_at": "b"},
        {"version": 2, "id": "validation", "label": "Validate inputs", "status": "running",
         "message": "Checking", "updated_at": ""},
    ]


def test_missing_and_invalid_progress_ids_keep_existing_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("MN_LAUNCH_PROGRESS_DIR", str(tmp_path / "missing"))
    progress.record_launch_progress(None, "launch", "running", "Ignored")
    assert not (tmp_path / "missing").exists()
    assert progress.read_launch_progress(None) == []
    assert progress.launch_progress_snapshot("missing")["status"] == "pending"
    assert progress.validate_progress_id(None) is None
    assert progress.validate_progress_id(" ") is None
    with pytest.raises(HTTPException) as error:
        progress.launch_progress_snapshot("../bad")
    assert error.value.status_code == 400
    assert error.value.detail == "invalid progress id"


def test_failed_progress_uses_latest_nonempty_failure():
    events = [
        {"phase": "launch", "status": "failed", "message": "First failure"},
        {"phase": "cleanup", "status": "failed", "detail": "Last failure"},
        {"phase": "cleanup", "status": "failed", "message": " "},
    ]
    assert progress.launch_progress_error(events) == "Last failure"
    assert progress.latest_terminal_launch_event(events) is events[0]
    assert progress.launch_progress_error([]) is None
    assert progress.latest_terminal_launch_event([]) is None
