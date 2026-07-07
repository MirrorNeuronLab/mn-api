from __future__ import annotations

from pathlib import Path

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


def runs_root() -> Path:
    return sdk_runs_root()


def run_dir_from_id(run_id: str | None, *, must_exist: bool = True) -> Path | None:
    return sdk_run_dir_from_id(run_id, must_exist=must_exist, root=runs_root())


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
