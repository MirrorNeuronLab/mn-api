"""HTTP projections for runs retained in replicated submission storage."""

from __future__ import annotations

from typing import Any

from mn_sdk.run_store import read_json_file, read_jsonl_file, runs_root
from mn_sdk.shared_run_store import shared_run_dir
from mn_sdk.workflow_progress import workflow_progress_snapshot


def stored_run(run_id: str) -> dict[str, Any] | None:
    directory = shared_run_dir(run_id)
    if directory is None:
        return None
    record = read_json_file(directory / "run.json")
    if record.get("run_id") != run_id:
        return None
    if record.get("status") not in {"completed", "failed", "cancelled"}:
        record = {**record, "status": "unknown"}
    return record


def stored_progress(run_id: str) -> dict[str, Any] | None:
    directory = shared_run_dir(run_id)
    record = stored_run(run_id)
    if directory is None or record is None:
        return None
    manifest: dict[str, Any] = {}
    for mapping_path in runs_root().glob("*/job.json"):
        if read_json_file(mapping_path).get("run_id") == run_id:
            manifest = read_json_file(mapping_path.parent / "manifest.json")
            break
    events = read_jsonl_file(directory / "events.jsonl", limit=5000)
    progress = workflow_progress_snapshot(manifest, events, job=record, job_id=run_id)
    progress["status"] = record["status"]
    return progress
