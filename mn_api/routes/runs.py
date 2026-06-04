from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import hashlib
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from mn_api import state
from mn_api.dependencies import require_auth


router = APIRouter(prefix="/api/v1")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_ARTIFACT_CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".gz": "application/gzip",
}


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


def _read_required_json_file(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return _read_json_file(path)


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _artifact_content_type(path: Path) -> str:
    return _ARTIFACT_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read {path.name}") from exc
    return digest.hexdigest()


def _artifact_id(path: Path, run_dir: Path) -> str:
    rel = path.relative_to(run_dir).as_posix()
    known = {
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
    if rel in known:
        return known[rel]
    rotated_id = _rotated_artifact_id(rel)
    if rotated_id:
        return rotated_id
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", rel).strip("_").lower()
    return normalized or "artifact"


def _rotated_artifact_id(rel: str) -> str | None:
    name = Path(rel).name
    match = re.match(r"^(events|logs|errors)\.(\d{3})\.jsonl(?:\.gz)?$", name)
    if not match:
        return None
    return f"{match.group(1)}_jsonl_{match.group(2)}"


def _artifact_ref(run_id: str, path: Path, run_dir: Path) -> dict[str, Any]:
    stat = path.stat()
    rel = path.relative_to(run_dir).as_posix()
    artifact_path = urllib.parse.quote(rel, safe="/")
    return {
        "artifact_id": _artifact_id(path, run_dir),
        "path": str(path),
        "relative_path": rel,
        "size_bytes": stat.st_size,
        "sha256": _sha256_file(path),
        "content_type": _artifact_content_type(path),
        "url": f"/api/v1/runs/{urllib.parse.quote(run_id)}/artifacts/{artifact_path}",
        "reveal_url": f"/api/v1/runs/{urllib.parse.quote(run_id)}/artifacts/{artifact_path}/reveal",
    }


def _list_artifact_files(run_dir: Path) -> list[Path]:
    if not run_dir.exists():
        return []
    files: list[Path] = []
    for path in run_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in _ARTIFACT_CONTENT_TYPES or path.name in {"result.json", "final_artifact.json"}:
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(run_dir).as_posix())


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


@router.get("/runs/{run_id}/result")
def get_run_result(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    return _read_required_json_file(run_dir / "result.json", "result")


@router.get("/runs/{run_id}/final-artifact")
def get_run_final_artifact(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    return _read_required_json_file(run_dir / "final_artifact.json", "final artifact")


@router.get("/runs/{run_id}/artifacts")
def list_run_artifacts(run_id: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    artifacts = [_artifact_ref(run_id, path, run_dir) for path in _list_artifact_files(run_dir)]
    return {"run_id": run_id, "run_dir": str(run_dir), "artifacts": artifacts}


@router.post("/runs/{run_id}/artifacts/{artifact_path:path}/reveal")
def reveal_run_artifact(run_id: str, artifact_path: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    path = _artifact_file_path(run_dir, artifact_path)
    try:
        _reveal_local_path(path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to open {path.name}") from exc
    return {"ok": True, "path": str(path), "folder": str(path.parent)}


@router.get("/runs/{run_id}/artifacts/{artifact_path:path}")
def get_run_artifact(run_id: str, artifact_path: str, _auth=Depends(require_auth)):
    run_dir = _ensure_run_exists(run_id)
    path = _artifact_file_path(run_dir, artifact_path)
    return FileResponse(path, media_type=_artifact_content_type(path))


@router.get("/runs/{run_id}/ui")
def get_run_ui(run_id: str, limit: int = Query(200, ge=0, le=1000), _auth=Depends(require_auth)):
    run_dir = _run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run not found")

    ui = _read_json_file(run_dir / "ui.json")
    service = _stored_web_ui_service(run_id, run_dir) or _registered_web_ui_service(run_id, run_dir)
    if not ui:
        if not service:
            raise HTTPException(status_code=404, detail="run UI not found")
        ui = _ui_from_service(service, run_id)
        web_ui = _web_ui_from_service(service, run_id, run_dir)
    else:
        web_ui = _read_json_file(run_dir / "web_ui.json") or (
            _web_ui_from_service(service, run_id, run_dir) if service else {}
        )

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ui": ui,
        "web_ui": web_ui,
        "job": _read_json_file(run_dir / "job.json"),
        "run": _read_json_file(run_dir / "run.json"),
        "events": _read_event_tail(run_dir / "events.jsonl", limit=limit),
    }


def _service_ui_fallback(run_id: str, run_dir: Path) -> dict[str, Any]:
    service = _stored_web_ui_service(run_id, run_dir) or _registered_web_ui_service(run_id, run_dir)
    if not service:
        return {}
    return {"ui": _ui_from_service(service, run_id), "web_ui": _web_ui_from_service(service, run_id, run_dir)}


def _service_web_ui_fallback(run_id: str, run_dir: Path) -> dict[str, Any]:
    service = _stored_web_ui_service(run_id, run_dir) or _registered_web_ui_service(run_id, run_dir)
    if not service:
        return {}
    return _web_ui_from_service(service, run_id, run_dir)


def _registered_web_ui_service(run_id: str, run_dir: Path) -> dict[str, Any]:
    job = _read_json_file(run_dir / "job.json")
    job_id = job.get("job_id") if isinstance(job.get("job_id"), str) else None
    try:
        response = state.client.resolve_service(
            "blueprint-web-ui",
            job_id=job_id,
            tags=["web_ui"],
            passing_only=False,
        )
        decoded = json.loads(response)
    except Exception:
        return {}
    services = decoded.get("services") if isinstance(decoded, dict) else []
    if not isinstance(services, list):
        return {}
    for service in services:
        if not isinstance(service, dict):
            continue
        meta = service.get("meta") if isinstance(service.get("meta"), dict) else {}
        if meta.get("run_id") == run_id or (job_id and service.get("job_id") == job_id):
            return service
    return {}


def _stored_web_ui_service(run_id: str, run_dir: Path) -> dict[str, Any]:
    job = _read_json_file(run_dir / "job.json")
    run = _read_json_file(run_dir / "run.json")
    event_relay = _read_json_file(run_dir / "event_relay.json")
    for record in (job, run, event_relay):
        service_info = _web_ui_service_info_from_record(record)
        if service_info:
            return _service_from_web_ui_service_info(service_info, run_id, run_dir, job)

    job_id = job.get("job_id") if isinstance(job.get("job_id"), str) else None
    if not job_id:
        return {}
    try:
        runtime_job = json.loads(state.client.get_job(job_id))
    except Exception:
        return {}
    service_info = _web_ui_service_info_from_record(runtime_job)
    if service_info:
        return _service_from_web_ui_service_info(service_info, run_id, run_dir, job)
    return {}


def _web_ui_service_info_from_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    for key in ("web_ui_service", "blueprint_web_ui_service"):
        value = record.get(key)
        if isinstance(value, dict) and value:
            return value
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    value = metadata.get("blueprint_web_ui_service")
    if isinstance(value, dict) and value:
        return value
    manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
    manifest_metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    value = manifest_metadata.get("blueprint_web_ui_service")
    if isinstance(value, dict) and value:
        return value
    service = record.get("service") if isinstance(record.get("service"), dict) else {}
    service_name = _first_string(service.get("service_name"), service.get("name"))
    if service and service_name == "blueprint-web-ui":
        return service
    nested_job = record.get("job") if isinstance(record.get("job"), dict) else {}
    if nested_job:
        return _web_ui_service_info_from_record(nested_job)
    return {}


def _service_from_web_ui_service_info(
    service_info: dict[str, Any],
    run_id: str,
    run_dir: Path,
    job: dict[str, Any],
) -> dict[str, Any]:
    name = _first_string(service_info.get("service_name"), service_info.get("name"), "blueprint-web-ui")
    address = _first_string(service_info.get("address"), service_info.get("host"), "127.0.0.1")
    port = _positive_port(service_info.get("port"))
    url = _first_string(service_info.get("url"))
    if not url and port:
        url = f"http://{address}:{port}"
    meta = dict(service_info)
    meta.setdefault("run_id", run_id)
    meta.setdefault("url", url)
    meta.setdefault("run_ui_path", str(run_dir / "ui.json"))
    meta.setdefault("web_ui_path", str(run_dir / "web_ui.json"))
    meta.setdefault("run_dir", str(run_dir))
    job_id = _first_string(job.get("job_id"), service_info.get("job_id"))
    service_id = _first_string(
        service_info.get("service_id"),
        service_info.get("id"),
        f"{job_id}:{service_info.get('node_id') or 'web_ui_dashboard'}:{name}" if job_id else "",
    )
    return {
        "id": service_id or None,
        "name": name,
        "job_id": job_id or None,
        "agent_id": service_info.get("node_id") or "web_ui_dashboard",
        "address": address,
        "port": port,
        "status": _web_ui_url_status(url) if url else "starting",
        "tags": ["web_ui", "blueprint", str(service_info.get("blueprint_id") or ""), "gradio"],
        "meta": meta,
        "source": "job_mapping",
    }


def _positive_port(value: Any) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _web_ui_url_status(url: str, *, timeout_seconds: float = 0.75) -> str:
    if not isinstance(url, str) or not url:
        return "starting"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "available"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "mn-api-web-ui-probe/1.0"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", 200)
            return "running" if int(status) < 500 else "starting"
    except urllib.error.HTTPError as exc:
        return "running" if int(exc.code) < 500 else "starting"
    except Exception:
        return "starting"


def _ui_from_service(service: dict[str, Any], run_id: str) -> dict[str, Any]:
    meta = service.get("meta") if isinstance(service.get("meta"), dict) else {}
    return {
        "schema_version": 1,
        "adapter": meta.get("adapter") or "gradio",
        "kind": "output",
        "title": meta.get("title") or "Blueprint Dashboard",
        "run_id": run_id,
        "blueprint_id": meta.get("blueprint_id"),
        "components": [
            {
                "type": "video",
                "label": "Live Stream Source",
                "browser_source": meta.get("browser_video_source") or "",
            },
            {
                "type": "events",
                "label": "Live Blueprint Event Stream",
                "max_events": 200,
            },
        ],
        "metadata": {
            "registered_by": "mirror_neuron_service_registry"
            if service.get("source") != "job_mapping"
            else "blueprint_job_mapping",
            "service_id": service.get("id"),
            "service_name": service.get("name"),
            "status": service.get("status"),
            "run_ui_path": meta.get("run_ui_path"),
        },
    }


def _web_ui_from_service(service: dict[str, Any], run_id: str, run_dir: Path) -> dict[str, Any]:
    meta = service.get("meta") if isinstance(service.get("meta"), dict) else {}
    url = meta.get("url")
    if not isinstance(url, str) or not url:
        address = service.get("address") or "127.0.0.1"
        port = service.get("port") or ""
        url = f"http://{address}:{port}" if port else ""
    return {
        "adapter": meta.get("adapter") or "gradio",
        "kind": "output",
        "url": url,
        "title": meta.get("title") or "Blueprint Dashboard",
        "path": str(run_dir),
        "status": "running" if service.get("status") == "passing" else str(service.get("status") or "starting"),
        "metadata": {
            "blueprint_id": meta.get("blueprint_id"),
            "run_id": run_id,
            "events_path": str(run_dir / "events.jsonl"),
            "ui_path": meta.get("run_ui_path") or str(run_dir / "ui.json"),
            "registered_by": "mirror_neuron_service_registry"
            if service.get("source") != "job_mapping"
            else "blueprint_job_mapping",
            "launch_adapter": "runtime_service",
            "service_id": service.get("id"),
            "service_name": service.get("name"),
            "browser_video_source": meta.get("browser_video_source"),
            "browser_publish_source": meta.get("browser_publish_source"),
        },
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


@router.get("/runs/{run_id}/timeline")
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


@router.get("/runs/{run_id}/observability-summary")
def get_run_observability_summary(run_id: str, _auth=Depends(require_auth)):
    _ensure_run_exists(run_id)
    tools = _observability_tools()
    summary = tools["read_run_observability_summary"](run_id, runs_root=_runs_root())
    if not summary:
        raise HTTPException(status_code=404, detail="observability summary not found")
    return summary


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
            read_run_observability_summary,
            read_run_resources,
            read_run_stream_records,
            read_run_timeline,
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
        "read_run_observability_summary": read_run_observability_summary,
        "read_run_resources": read_run_resources,
        "read_run_stream_records": read_run_stream_records,
        "read_run_timeline": read_run_timeline,
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
