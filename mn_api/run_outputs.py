from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any


OUTPUT_CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".gz": "application/gzip",
}


def output_content_type(path: Path) -> str:
    return OUTPUT_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def output_refs(run_id: str, run_dir: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in _recorded_output_items(run_dir):
        path = _recorded_output_path(item)
        if path is None or not path.is_file():
            continue
        refs.append(_output_ref(run_id, len(refs), path, item))
    return refs


def output_path_by_index(run_dir: Path, output_index: int) -> Path | None:
    if output_index < 0:
        return None
    refs = output_refs("", run_dir)
    if output_index >= len(refs):
        return None
    return Path(refs[output_index]["path"]).expanduser().resolve()


def _output_ref(run_id: str, index: int, path: Path, item: dict[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    kind = _first_string(item.get("kind"), item.get("artifact_id"), path.stem)
    name = path.name
    quoted_run_id = urllib.parse.quote(run_id)
    artifact_id = f"output_{index}_{_slug(kind or name)}"
    return {
        "artifact_id": artifact_id,
        "kind": kind or None,
        "name": name,
        "label": name,
        "path": str(path),
        "relative_path": name,
        "source": "post_launch_output",
        "external": True,
        "size_bytes": stat.st_size,
        "sha256": _sha256_file(path),
        "content_type": output_content_type(path),
        "url": f"/api/v1/runs/{quoted_run_id}/outputs/{index}",
        "reveal_url": f"/api/v1/runs/{quoted_run_id}/outputs/{index}/reveal",
    }


def _recorded_output_items(run_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for payload in _output_payloads(run_dir):
        for item in _output_items_from_payload(payload):
            path = _recorded_output_path(item)
            if path is None:
                continue
            key = str(path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            items.append(item)
    return items


def _output_payloads(run_dir: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for name in [
        "post_launch_materialized.json",
        "post_launch_state.json",
        "result.json",
        "final_artifact.json",
    ]:
        payload = _read_json(run_dir / name)
        if payload:
            payloads.append(payload)
    return payloads


def _output_items_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    _extend_output_items(items, payload.get("output_files"))
    final_artifact = payload.get("final_artifact")
    if isinstance(final_artifact, dict):
        _extend_output_items(items, final_artifact.get("output_files"))
    result = payload.get("result")
    if isinstance(result, dict):
        _extend_output_items(items, result.get("output_files"))
        nested_artifact = result.get("final_artifact")
        if isinstance(nested_artifact, dict):
            _extend_output_items(items, nested_artifact.get("output_files"))
    return items


def _extend_output_items(items: list[dict[str, Any]], value: Any) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            items.append(item)
        elif isinstance(item, str) and item.strip():
            items.append({"path": item.strip()})


def _recorded_output_path(item: dict[str, Any]) -> Path | None:
    value = _first_string(item.get("path"), item.get("file"), item.get("file_path"), item.get("local_path"))
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower() or "output"
