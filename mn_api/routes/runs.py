from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

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


@router.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    limit: int = Query(200, ge=0, le=5000),
    channel: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    if channel == "human":
        events = tools["read_human_events"](run_id, runs_root=_runs_root(), limit=limit)
    else:
        events = tools["read_run_events"](run_id, runs_root=_runs_root(), limit=limit)
        if channel:
            events = [event for event in events if event.get("channel") == channel]
    return {"run_id": run_id, "data": events[-limit:] if limit else events}


@router.get("/runs/{run_id}/logs")
def get_run_logs(
    run_id: str,
    level: str | None = Query(default=None),
    limit: int = Query(200, ge=0, le=5000),
    since: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return {
        "run_id": run_id,
        "data": tools["read_run_logs"](
            run_id,
            runs_root=_runs_root(),
            level=level,
            limit=limit,
            since=since,
        ),
    }


@router.get("/runs/{run_id}/stream")
def stream_run_observability(
    run_id: str,
    channels: str = Query("events,logs,human,resources"),
    level: str | None = Query(default=None),
    interval: float = Query(1.0, ge=1.0, le=60.0),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    selected_channels = [item.strip() for item in channels.split(",") if item.strip()]

    def event_source():
        tools = _observability_tools()
        seen: set[str] = set()
        last_ts: str | None = None
        while True:
            records = tools["read_run_stream_records"](
                run_id,
                runs_root=_runs_root(),
                channels=selected_channels,
                level=level,
                limit=500,
                since=last_ts,
            )
            emitted = False
            for record in records:
                record_id = str(record.get("id") or f"{record.get('channel')}:{record.get('ts')}")
                if record_id in seen:
                    continue
                seen.add(record_id)
                if len(seen) > 5000:
                    seen.clear()
                last_ts = str(record.get("ts") or last_ts or "")
                emitted = True
                yield f"id: {record_id}\nevent: {record.get('channel') or 'message'}\ndata: {json.dumps(record, sort_keys=True)}\n\n"
            if not emitted:
                yield ": heartbeat\n\n"
            time.sleep(interval)

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/runs/{run_id}/resources")
def get_run_resources(
    run_id: str,
    window: str = Query("24h"),
    bucket: str = Query("1h"),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return tools["read_run_resources"](
        run_id,
        runs_root=_runs_root(),
        window_hours=_duration_seconds(window) / 3600.0,
        bucket_seconds=max(int(_duration_seconds(bucket)), 1),
    )


@router.get("/runs/{run_id}/resources/stream")
def stream_run_resources(
    run_id: str,
    interval: float = Query(5.0, ge=1.0, le=60.0),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)

    def event_source():
        tools = _observability_tools()
        while True:
            payload = tools["read_run_resources"](run_id, runs_root=_runs_root())
            yield f"event: resources\ndata: {json.dumps(payload, sort_keys=True)}\n\n"
            time.sleep(interval)

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/runs/{run_id}/human")
def get_run_human_events(
    run_id: str,
    status: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    events = (
        tools["list_pending_human_requests"](run_id, runs_root=_runs_root())
        if status == "pending"
        else tools["read_human_events"](run_id, runs_root=_runs_root(), status=status)
    )
    return {"run_id": run_id, "data": events}


@router.post("/runs/{run_id}/human/{request_id}/response")
def post_run_human_response(
    run_id: str,
    request_id: str,
    payload: dict[str, Any],
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return tools["record_human_response"](run_id, request_id, payload, runs_root=_runs_root())


@router.post("/runs/{run_id}/human/{notice_id}/ack")
def post_run_human_ack(
    run_id: str,
    notice_id: str,
    payload: dict[str, Any] | None = None,
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return tools["acknowledge_human_notice"](run_id, notice_id, payload or {}, runs_root=_runs_root())


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


def _ensure_run_exists(run_id: str) -> Path:
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run not found")
    return run_dir


def _observability_tools() -> dict[str, Any]:
    _ensure_blueprint_support_path()
    try:
        from mn_blueprint_support.observability import (
            acknowledge_human_notice,
            list_pending_human_requests,
            read_human_events,
            read_run_events,
            read_run_logs,
            read_run_resources,
            read_run_stream_records,
            record_human_response,
        )
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=500, detail="blueprint observability support is unavailable") from exc
    return {
        "acknowledge_human_notice": acknowledge_human_notice,
        "list_pending_human_requests": list_pending_human_requests,
        "read_human_events": read_human_events,
        "read_run_events": read_run_events,
        "read_run_logs": read_run_logs,
        "read_run_resources": read_run_resources,
        "read_run_stream_records": read_run_stream_records,
        "record_human_response": record_human_response,
    }


def _ensure_blueprint_support_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    support_src = repo_root / "mn-skills" / "blueprint_support_skill" / "src"
    if support_src.exists() and str(support_src) not in sys.path:
        sys.path.insert(0, str(support_src))


def _duration_seconds(value: str) -> float:
    text = str(value).strip().lower()
    if not text:
        raise HTTPException(status_code=400, detail="duration cannot be empty")
    unit = text[-1]
    number_text = text[:-1] if unit.isalpha() else text
    try:
        number = float(number_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid duration: {value}") from exc
    if unit == "s" or not unit.isalpha():
        return number
    if unit == "m":
        return number * 60
    if unit == "h":
        return number * 3600
    if unit == "d":
        return number * 86400
    raise HTTPException(status_code=400, detail=f"unsupported duration unit: {unit}")
