from __future__ import annotations

import json
import os
import re
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from mn_api.dependencies import require_auth


router = APIRouter(prefix="/api/v1")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def _runs_root() -> Path:
    return Path(os.getenv("MN_RUNS_ROOT") or "~/.mn/runs").expanduser().resolve()


def _run_dir(run_id: str) -> Path:
    if not _SAFE_RUN_ID.match(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")
    root = _runs_root()
    run_dir = (root / run_id).resolve()
    if not run_dir.is_relative_to(root):
        raise HTTPException(status_code=400, detail="invalid run id")
    return run_dir


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"failed to read {path.name}") from exc
    return payload if isinstance(payload, dict) else {}


def _read_event_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    events: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    event = {"type": "unparseable_event", "payload": {"line": stripped}}
                if isinstance(event, dict):
                    events.append(event)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="failed to read events") from exc
    return list(events)


def _first_video_source(ui: dict[str, Any]) -> str:
    components = ui.get("components") if isinstance(ui.get("components"), list) else []
    for component in components:
        if not isinstance(component, dict):
            continue
        if component.get("type") == "video" and component.get("source"):
            return str(component["source"])
    return ""


def _local_source_path(source: str, run_dir: Path) -> Path | None:
    if not source:
        return None
    if source.startswith("file://"):
        parsed = urllib.parse.urlparse(source)
        return Path(urllib.parse.unquote(parsed.path)).expanduser().resolve()
    if "://" in source:
        return None
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    return candidate.resolve()


def _allowed_local_roots(run_dir: Path, ui: dict[str, Any]) -> list[Path]:
    metadata = ui.get("metadata") if isinstance(ui.get("metadata"), dict) else {}
    roots = [run_dir.resolve()]
    bundle_dir = metadata.get("bundle_dir")
    if isinstance(bundle_dir, str) and bundle_dir:
        roots.append(Path(bundle_dir).expanduser().resolve())
    return roots


def _is_allowed_local_path(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


@router.get("/runs/{run_id}/ui")
def get_run_ui(run_id: str, limit: int = Query(200, ge=0, le=1000), _auth=Depends(require_auth)):
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run not found")

    ui = _read_json_file(run_dir / "ui.json")
    if not ui:
        raise HTTPException(status_code=404, detail="run UI not found")

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ui": ui,
        "web_ui": _read_json_file(run_dir / "web_ui.json"),
        "job": _read_json_file(run_dir / "job.json"),
        "run": _read_json_file(run_dir / "run.json"),
        "events": _read_event_tail(run_dir / "events.jsonl", limit=limit),
    }


@router.get("/runs/{run_id}/ui/video")
def get_run_ui_video(run_id: str, _auth=Depends(require_auth)):
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run not found")

    ui = _read_json_file(run_dir / "ui.json")
    if not ui:
        raise HTTPException(status_code=404, detail="run UI not found")

    source_path = _local_source_path(_first_video_source(ui), run_dir)
    if source_path is None:
        raise HTTPException(status_code=404, detail="local video not configured")
    if not _is_allowed_local_path(source_path, _allowed_local_roots(run_dir, ui)):
        raise HTTPException(status_code=403, detail="video source is outside allowed roots")
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=404, detail="video source not found")
    return FileResponse(source_path)
