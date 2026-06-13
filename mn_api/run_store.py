from __future__ import annotations

import gzip
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any


SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
DEFAULT_RUNS_ROOT = "~/.mn/runs"
DEFAULT_JSONL_LIMIT = 5000
DEFAULT_MAX_LINE_CHARS = 2000


def runs_root() -> Path:
    return Path(os.getenv("MN_RUNS_ROOT") or DEFAULT_RUNS_ROOT).expanduser().resolve()


def run_dir_from_id(run_id: str | None, *, must_exist: bool = True) -> Path | None:
    if not run_id or not SAFE_RUN_ID.match(run_id):
        return None
    root = runs_root()
    candidate = (root / run_id).resolve()
    if not candidate.is_relative_to(root):
        return None
    if must_exist and not candidate.exists():
        return None
    return candidate


def read_json_file(path: Path, *, raise_on_error: bool = False, error_detail: str | None = None) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if raise_on_error:
            raise RuntimeError(error_detail or f"failed to read {path.name}") from exc
        return {}
    return payload if isinstance(payload, dict) else {}


def read_jsonl_file(
    path: Path,
    *,
    limit: int = DEFAULT_JSONL_LIMIT,
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    events: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        opener = gzip.open if path.suffix == ".gz" else Path.open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    event = {"type": "unparseable_event", "payload": {"line": stripped[:max_line_chars]}}
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return list(events)


def stream_jsonl_files(run_dir: Path, file_name: str) -> list[Path]:
    paths: list[Path] = []
    index_path = run_dir / f"{Path(file_name).stem}.index.json"
    if index_path.exists():
        index = read_json_file(index_path)
        for segment in index.get("segments") or []:
            if isinstance(segment, dict) and segment.get("path"):
                segment_path = run_dir / str(segment["path"])
                if not segment_path.exists() and segment_path.suffix != ".gz":
                    compressed = segment_path.with_suffix(segment_path.suffix + ".gz")
                    if compressed.exists():
                        segment_path = compressed
                paths.append(segment_path)
    paths.append(run_dir / file_name)
    return paths


def first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
