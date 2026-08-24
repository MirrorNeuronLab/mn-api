from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from mn_sdk.blueprint_support.observability import (
    acknowledge_human_notice,
    list_pending_human_requests,
    list_runs,
    load_run,
    read_human_events,
    read_run_events,
    read_run_logs,
    read_run_observability_summary,
    read_run_resources,
    read_run_stream_records,
    read_run_timeline,
    record_human_response,
)
from mn_api.artifacts import artifact_content_type, artifact_ref, list_artifact_files
from mn_api.dependencies import require_auth
from mn_api.run_outputs import output_content_type, output_path_by_index, output_refs
from mn_api.run_store import read_json_file as _read_json_object
from mn_api.run_store import run_dir_from_id
from mn_api.run_store import runs_root as _runs_root
from mn_api.schemas import RunCompareRequest


# internal router is intentionally not mounted.


def list_blueprint_runs(
    blueprint_id: str | None = Query(default=None),
    limit: int = Query(20, ge=1, le=500),
    _auth=Depends(require_auth),
):
    return {"data": list_runs(runs_root=_runs_root(), blueprint_id=blueprint_id, limit=limit)}


def compare_runs(req: RunCompareRequest, _auth=Depends(require_auth)):
    try:
        record_a = load_run(req.run_a, runs_root=_runs_root(), include_observability=True)
        record_b = load_run(req.run_b, runs_root=_runs_root(), include_observability=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _run_compare_payload(req.run_a, record_a, req.run_b, record_b)


def _run_dir(run_id: str) -> Path:
    run_dir = run_dir_from_id(run_id, must_exist=False)
    if run_dir is None:
        raise HTTPException(status_code=400, detail="invalid run id")
    return run_dir


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return _read_json_object(path, raise_on_error=True, error_detail=f"failed to read {path.name}")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _read_required_json_file(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return _read_json_file(path)


def _artifact_ref(run_id: str, path: Path, run_dir: Path) -> dict[str, Any]:
    try:
        return artifact_ref(run_id, path, run_dir)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read {path.name}") from exc


def _artifact_file_path(run_dir: Path, artifact_path: str) -> Path:
    if not artifact_path:
        raise HTTPException(status_code=400, detail="artifact path is required")
    candidate = (run_dir / urllib.parse.unquote(artifact_path)).resolve()
    if not candidate.is_relative_to(run_dir):
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return candidate


def _reveal_local_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
        return
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path.parent)])


def get_run_result(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    return _read_required_json_file(run_dir / "result.json", "result")


def get_run_final_artifact(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    return _read_required_json_file(run_dir / "final_artifact.json", "final artifact")


def export_run(
    run_id: str,
    format: str = Query("json"),
    _auth=Depends(require_auth),
):
    try:
        record = load_run(run_id, runs_root=_runs_root(), include_observability=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    normalized_format = format.lower().strip()
    if normalized_format == "json":
        return record
    if normalized_format in {"markdown", "md"}:
        return Response(content=_render_markdown_export(record), media_type="text/markdown")
    raise HTTPException(status_code=400, detail="unsupported export format")


def list_run_artifacts(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    artifacts = [_artifact_ref(run_id, path, run_dir) for path in list_artifact_files(run_dir)]
    artifacts.extend(output_refs(run_id, run_dir))
    return {"run_id": run_id, "run_dir": str(run_dir), "artifacts": artifacts}


def reveal_run_artifact(run_id: str, artifact_path: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    path = _artifact_file_path(run_dir, artifact_path)
    try:
        _reveal_local_path(path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to open {path.name}") from exc
    return {"ok": True, "path": str(path), "folder": str(path.parent)}


def get_run_artifact(run_id: str, artifact_path: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    path = _artifact_file_path(run_dir, artifact_path)
    return FileResponse(path, media_type=artifact_content_type(path))


def list_run_outputs(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    return {"run_id": run_id, "run_dir": str(run_dir), "outputs": output_refs(run_id, run_dir)}


def reveal_run_output(run_id: str, output_index: int, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    path = output_path_by_index(run_dir, output_index)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    try:
        _reveal_local_path(path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to open {path.name}") from exc
    return {"ok": True, "path": str(path), "folder": str(path.parent)}


def get_run_output(run_id: str, output_index: int, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    path = output_path_by_index(run_dir, output_index)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(path, media_type=output_content_type(path), filename=path.name)


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


def get_run_timeline(
    run_id: str,
    limit: int = Query(500, ge=0, le=5000),
    since: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return {
        "run_id": run_id,
        "data": tools["read_run_timeline"](
            run_id,
            runs_root=_runs_root(),
            limit=limit,
            since=since,
        ),
    }


def get_run_observability_summary(run_id: str, _auth=Depends(require_auth)):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    summary = tools["read_run_observability_summary"](run_id, runs_root=_runs_root())
    if not summary:
        raise HTTPException(status_code=404, detail="observability summary not found")
    return summary


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


def post_run_human_response(
    run_id: str,
    request_id: str,
    payload: dict[str, Any],
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return tools["record_human_response"](run_id, request_id, payload, runs_root=_runs_root())


def post_run_human_ack(
    run_id: str,
    notice_id: str,
    payload: dict[str, Any] | None = None,
    _auth=Depends(require_auth),
):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    return tools["acknowledge_human_notice"](run_id, notice_id, payload or {}, runs_root=_runs_root())


def _ensure_run_exists(run_id: str) -> Path:
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run not found")
    return run_dir


def _observability_tools() -> dict[str, Any]:
    return {
        "acknowledge_human_notice": acknowledge_human_notice,
        "list_pending_human_requests": list_pending_human_requests,
        "read_human_events": read_human_events,
        "read_run_events": read_run_events,
        "read_run_logs": read_run_logs,
        "read_run_observability_summary": read_run_observability_summary,
        "read_run_resources": read_run_resources,
        "read_run_stream_records": read_run_stream_records,
        "read_run_timeline": read_run_timeline,
        "record_human_response": record_human_response,
    }


def _run_compare_payload(run_a: str, record_a: dict[str, Any], run_b: str, record_b: dict[str, Any]) -> dict[str, Any]:
    summary_a = _run_summary(record_a.get("run") or {})
    summary_b = _run_summary(record_b.get("run") or {})
    artifact_a = _final_artifact(record_a)
    artifact_b = _final_artifact(record_b)
    scalar_keys = sorted(set(artifact_a) | set(artifact_b))
    artifact_diff = {
        key: {run_a: artifact_a.get(key), run_b: artifact_b.get(key)}
        for key in scalar_keys
        if not isinstance(artifact_a.get(key), (dict, list)) and not isinstance(artifact_b.get(key), (dict, list))
    }
    return {
        "runs": {
            run_a: {**summary_a, "event_count": len(record_a.get("events") or []), "final_artifact": artifact_a},
            run_b: {**summary_b, "event_count": len(record_b.get("events") or []), "final_artifact": artifact_b},
        },
        "artifact_diff": artifact_diff,
    }


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "blueprint_id": run.get("blueprint_id"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "run_dir": run.get("run_dir"),
    }


def _final_artifact(record: dict[str, Any]) -> dict[str, Any]:
    artifact = record.get("final_artifact") or {}
    if isinstance(artifact, dict) and artifact:
        return artifact
    result = record.get("result") or {}
    nested = result.get("final_artifact") if isinstance(result, dict) else None
    return nested if isinstance(nested, dict) else {}


def _render_markdown_export(record: dict[str, Any]) -> str:
    run = record.get("run") or {}
    lines = [
        f"# Blueprint Run {run.get('run_id') or ''}".rstrip(),
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for key, value in _run_summary(run).items():
        lines.append(f"| {key} | {_markdown_cell(value)} |")
    lines.extend(
        [
            "",
            "## Final Artifact",
            "",
            "```json",
            json.dumps(_final_artifact(record), indent=2, sort_keys=True),
            "```",
            "",
            "## Event Tail",
            "",
            "```json",
            json.dumps((record.get("events") or [])[-50:], indent=2, sort_keys=True),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _markdown_cell(value: Any) -> str:
    text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")
    return text.replace("|", "\\|").replace("\n", " ")


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
