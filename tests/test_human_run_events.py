"""Human responses follow the authoritative mapped run ledger."""
import pytest
from fastapi import HTTPException
from mn_sdk.blueprint_support import append_human_event, list_pending_human_requests

from mn_api.routes import runs


def test_completed_shared_run_human_choice_is_read_and_recorded_in_shared_ledger(tmp_path, monkeypatch):
    local = tmp_path / "local" / "run-1"
    shared = tmp_path / "shared" / "run-1"
    local.mkdir(parents=True)
    shared.mkdir(parents=True)
    append_human_event(
        "run-1",
        "human_input_requested",
        {"request_id": "review-1", "prompt": "Which direction?", "options": ["Investigate further"]},
        runs_root=shared.parent,
    )
    monkeypatch.setattr(runs, "_ensure_run_exists", lambda _run_id: local)
    monkeypatch.setattr(runs, "shared_run_dir", lambda _run_id: shared)

    events = runs.get_run_human_events("run-1", status="pending")
    assert events["data"][0]["payload"]["request_id"] == "review-1"
    recorded = runs.post_run_human_response(
        "run-1", "review-1", {"response": {"decision": "respond", "action": "Investigate further"}}
    )
    assert recorded["payload"]["request_id"] == "review-1"
    assert list_pending_human_requests("run-1", runs_root=shared.parent) == []
    assert not (local / "human.jsonl").exists()
    with pytest.raises(HTTPException) as repeated:
        runs.post_run_human_response("run-1", "review-1", {"response": {"action": "Defer"}})
    assert repeated.value.status_code == 409


def test_local_run_human_choice_stays_local_when_no_mapped_shared_run(tmp_path, monkeypatch):
    local = tmp_path / "runs" / "run-1"
    local.mkdir(parents=True)
    append_human_event(
        "run-1",
        "human_input_requested",
        {"request_id": "review-1", "prompt": "What next?"},
        runs_root=local.parent,
    )
    monkeypatch.setattr(runs, "_ensure_run_exists", lambda _run_id: local)
    monkeypatch.setattr(runs, "shared_run_dir", lambda _run_id: None)

    recorded = runs.post_run_human_response(
        "run-1", "review-1", {"response": {"decision": "respond", "action": "Defer"}}
    )
    assert recorded["payload"]["response"]["action"] == "Defer"
    assert (local / "human.jsonl").is_file()
