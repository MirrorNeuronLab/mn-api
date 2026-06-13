from __future__ import annotations

import hashlib
import re
import urllib.parse
from pathlib import Path
from typing import Any


ARTIFACT_CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".gz": "application/gzip",
}

KNOWN_ARTIFACT_IDS = {
    "result.json": "result_json",
    "final_artifact.json": "final_artifact_json",
    "events.jsonl": "events_jsonl",
    "logs.jsonl": "logs_jsonl",
    "errors.jsonl": "errors_jsonl",
    "timeline.jsonl": "timeline_jsonl",
    "timeline.json": "timeline_json",
    "observability_summary.json": "observability_summary_json",
    "events.log": "events_log",
    "errors.log": "errors_log",
    "events.index.json": "events_index_json",
    "resources.jsonl": "resources_jsonl",
    "errors.index.json": "errors_index_json",
    "human.jsonl": "human_events_jsonl",
    "job.json": "job_json",
    "run.json": "run_json",
    "ui.json": "ui_json",
    "web_ui.json": "web_ui_json",
}


def artifact_content_type(path: Path) -> str:
    return ARTIFACT_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_id(path: Path, run_dir: Path) -> str:
    rel = path.relative_to(run_dir).as_posix()
    if rel in KNOWN_ARTIFACT_IDS:
        return KNOWN_ARTIFACT_IDS[rel]
    rotated_id = rotated_artifact_id(rel)
    if rotated_id:
        return rotated_id
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", rel).strip("_").lower()
    return normalized or "artifact"


def rotated_artifact_id(rel: str) -> str | None:
    name = Path(rel).name
    match = re.match(r"^(events|logs|errors)\.(\d{3})\.jsonl(?:\.gz)?$", name)
    if not match:
        return None
    return f"{match.group(1)}_jsonl_{match.group(2)}"


def artifact_ref(run_id: str, path: Path, run_dir: Path) -> dict[str, Any]:
    stat = path.stat()
    rel = path.relative_to(run_dir).as_posix()
    artifact_path = urllib.parse.quote(rel, safe="/")
    quoted_run_id = urllib.parse.quote(run_id)
    return {
        "artifact_id": artifact_id(path, run_dir),
        "path": str(path),
        "relative_path": rel,
        "size_bytes": stat.st_size,
        "sha256": file_sha256(path),
        "content_type": artifact_content_type(path),
        "url": f"/api/v1/runs/{quoted_run_id}/artifacts/{artifact_path}",
        "reveal_url": f"/api/v1/runs/{quoted_run_id}/artifacts/{artifact_path}/reveal",
    }


def list_artifact_files(run_dir: Path) -> list[Path]:
    if not run_dir.exists():
        return []
    files: list[Path] = []
    for path in run_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in ARTIFACT_CONTENT_TYPES or path.name in {"result.json", "final_artifact.json"}:
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(run_dir).as_posix())
