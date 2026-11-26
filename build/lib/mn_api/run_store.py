from __future__ import annotations

from pathlib import Path
from mn_sdk import RuntimeConfig

from mn_sdk.run_store import (
    DEFAULT_JSONL_LIMIT,
    DEFAULT_MAX_LINE_CHARS,
    SAFE_RUN_ID,
    first_string,
    read_json_file,
    read_jsonl_file,
    run_dir_from_id as sdk_run_dir_from_id,
    runs_root as sdk_runs_root,
    stream_jsonl_files,
)
from mn_sdk.shared_run_store import shared_run_dir


def runs_root() -> Path:
    return sdk_runs_root()


def run_dir_from_id(run_id: str | None, *, must_exist: bool = True) -> Path | None:
    local = sdk_run_dir_from_id(run_id, must_exist=must_exist, root=runs_root())
    if local is not None:
        return local
    if must_exist and run_id:
        return shared_run_dir(run_id, local_root=runs_root())
    return None


def shared_result_events(job_id: str, run_id: str, *, limit: int = 5000) -> list[dict]:
    """Read result events from a run's job-owned shared submission while it is active."""

    if not SAFE_RUN_ID.fullmatch(job_id) or not SAFE_RUN_ID.fullmatch(run_id):
        return []
    submissions = Path(RuntimeConfig.from_env().shared_storage_root).expanduser().resolve() / "submissions"
    if not submissions.is_dir():
        return []
    results = []
    for submission in submissions.glob(f"{job_id}-def-*"):
        events_path = submission / "outputs" / "runs" / run_id / "events.jsonl"
        if not events_path.resolve().is_relative_to(submissions):
            continue
        for event in read_jsonl_file(events_path, limit=limit):
            payload = event.get("payload") if isinstance(event, dict) else None
            if event.get("type") == "run_result_available" and isinstance(payload, dict) and payload.get("run_id") == run_id:
                results.append(event)
    return results[-limit:]


__all__ = [
    "DEFAULT_JSONL_LIMIT",
    "DEFAULT_MAX_LINE_CHARS",
    "SAFE_RUN_ID",
    "first_string",
    "read_json_file",
    "read_jsonl_file",
    "run_dir_from_id",
    "runs_root",
    "stream_jsonl_files",
]
