from __future__ import annotations

import json
import os
import re
import urllib.parse
import hashlib
from collections import Counter, deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from mn_sdk import (
    make_validation_report,
    run_input_validation,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
)

from mn_api import state
from mn_api.agent_graph import build_agent_graph
from mn_api.blueprints import cleanup_blueprint_processes_for_job
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
from mn_api.schemas import SubmitJobRequest


router = APIRouter(prefix="/api/v1")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_MAX_COMPACT_STRING = 2000
_MAX_COMPACT_LIST = 25
_MAX_COMPACT_DEPTH = 5
_BLOB_KEYS = {
    "logs",
    "log",
    "stdout",
    "stderr",
    "content",
    "file_data",
    "fileData",
    "pdf_bytes",
    "pdfBytes",
    "bytes",
    "data_uri",
    "base64",
    "payloads_bytes",
    "final_artifact",
    "finalArtifact",
    "result",
}
_ARTIFACT_CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
}


@router.post("/jobs")
def submit_job(req: SubmitJobRequest, _auth=Depends(require_auth)):
    try:
        if req.bundle_path:
            manifest_json, payloads_bytes = load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
            state.close_client()
            validation_response = _validate_job_bundle(req.bundle_path, manifest_json, force=req.force)
            if validation_response is not None:
                return validation_response
        elif req.manifest_json is not None:
            manifest_json = req.manifest_json
            payloads_bytes = (
                {key: value.encode("utf-8") for key, value in req.payloads.items()}
                if req.payloads
                else {}
            )
            state.close_client()
            validation_response = _validate_job_manifest(manifest_json, force=req.force)
            if validation_response is not None:
                return validation_response
        else:
            raise HTTPException(
                status_code=422,
                detail="manifest_json or _bundle_path is required",
            )

        if req.force:
            job_id = state.client.submit_job(manifest_json, payloads_bytes, force=True)
        else:
            job_id = state.client.submit_job(manifest_json, payloads_bytes)
        return {"id": job_id, "status": "pending"}
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


def _validate_job_bundle(bundle_path: str, manifest_json: str, *, force: bool):
    if force:
        return None
    manifest = _decode_manifest(manifest_json)
    spec_issues = validate_requirements_spec_issues(manifest) + validate_input_validation_spec_issues(manifest)
    if spec_issues:
        return validation_problem_response(
            make_validation_report(spec_issues),
            status_code=422,
            error="manifest_validation_failed",
            title="Manifest validation failed",
            detail="Fix the highlighted manifest fields and submit again.",
        )
    result = run_input_validation(Path(bundle_path), manifest)
    if not result.get("ok"):
        return validation_problem_response(
            result,
            status_code=422,
            error="input_validation_failed",
            title="Input validation failed",
            detail="Fix the highlighted input fields and submit again.",
        )
    return None


def _validate_job_manifest(manifest_json: str, *, force: bool):
    if force:
        return None
    manifest = _decode_manifest(manifest_json)
    spec_issues = validate_requirements_spec_issues(manifest) + validate_input_validation_spec_issues(manifest)
    if spec_issues:
        return validation_problem_response(
            make_validation_report(spec_issues),
            status_code=422,
            error="manifest_validation_failed",
            title="Manifest validation failed",
            detail="Fix the highlighted manifest fields and submit again.",
        )
    validation = run_input_validation(Path.cwd(), manifest)
    if not validation.get("ok"):
        return validation_problem_response(
            validation,
            status_code=422,
            error="input_validation_failed",
            title="Input validation failed",
            detail="Fix the highlighted input fields and submit again.",
        )
    return None


def _decode_manifest(manifest_json: str) -> dict:
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="manifest_json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="manifest_json must be an object")
    return manifest


@router.get("/jobs")
def list_jobs(limit: int = 20, include_terminal: bool = True, _auth=Depends(require_auth)):
    try:
        jobs_json = state.client.list_jobs(limit, include_terminal)
        return json.loads(jobs_json)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs:cleanup")
@router.post("/jobs/cleanup")
def cleanup_jobs(_auth=Depends(require_auth)):
    try:
        cleared_count = state.client.clear_jobs()
        return {"cleared_count": cleared_count}
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, include: str = Query("compact"), _auth=Depends(require_auth)):
    try:
        if include == "full":
            job_json = state.client.get_job(job_id)
            return json.loads(job_json)
        if include not in {"compact", "summary"}:
            raise HTTPException(status_code=400, detail="include must be 'compact', 'summary', or 'full'")
        return _compact_job_detail(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


def _runs_root() -> Path:
    return Path(os.getenv("MN_RUNS_ROOT") or "~/.mn/runs").expanduser().resolve()


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_dir_from_id(run_id: str | None) -> Path | None:
    if not run_id or not _SAFE_RUN_ID.match(run_id):
        return None
    root = _runs_root()
    candidate = (root / run_id).resolve()
    if not candidate.is_relative_to(root) or not candidate.exists():
        return None
    return candidate


def _job_record_matches(job_record: dict[str, Any], job_id: str) -> bool:
    nested = job_record.get("job") if isinstance(job_record.get("job"), dict) else {}
    candidates = [
        job_record.get("job_id"),
        job_record.get("id"),
        nested.get("job_id"),
        nested.get("id"),
    ]
    return any(str(candidate) == job_id for candidate in candidates if candidate)


def _find_run_dir_for_job(job_id: str) -> tuple[Path | None, dict[str, Any]]:
    root = _runs_root()
    if not root.exists():
        return None, {}
    try:
        candidates = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None, {}
    for run_dir in candidates:
        job_record = _read_json_file(run_dir / "job.json")
        if _job_record_matches(job_record, job_id):
            return run_dir, job_record
    return None, {}


def _stream_job_events(job_id: str, *, limit: int = 200) -> tuple[list[dict[str, Any]], str | None]:
    events: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        for event_json in state.client.stream_events(job_id):
            try:
                event = json.loads(event_json)
            except json.JSONDecodeError:
                event = {"type": "unparseable_event", "message": str(event_json)[:_MAX_COMPACT_STRING]}
            if isinstance(event, dict):
                events.append(event)
    except Exception as exc:
        return list(events), str(exc)
    return list(events), None


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_nested_string(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            found = _extract_nested_string(candidate, *keys)
            if found:
                return found
    if isinstance(value, list):
        for candidate in value[:_MAX_COMPACT_LIST]:
            found = _extract_nested_string(candidate, *keys)
            if found:
                return found
    return ""


def _compact_blob(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "omitted": True,
            "type": "string",
            "chars": len(value),
            "preview": value[:200],
        }
    if isinstance(value, bytes):
        return {"omitted": True, "type": "bytes", "bytes": len(value)}
    if isinstance(value, dict):
        return {"omitted": True, "type": "object", "keys": sorted(str(key) for key in value.keys())[:25]}
    if isinstance(value, list):
        return {"omitted": True, "type": "array", "items": len(value)}
    return {"omitted": True, "type": type(value).__name__}


def _compact_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_COMPACT_DEPTH:
        return _compact_blob(value)
    if isinstance(value, str):
        if len(value) > _MAX_COMPACT_STRING:
            return {
                "truncated": True,
                "chars": len(value),
                "preview": value[:_MAX_COMPACT_STRING],
            }
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"omitted": True, "type": "bytes", "bytes": len(value)}
    if isinstance(value, list):
        items = [_compact_value(item, depth + 1) for item in value[:_MAX_COMPACT_LIST]]
        if len(value) > _MAX_COMPACT_LIST:
            items.append({"omitted_items": len(value) - _MAX_COMPACT_LIST})
        return items
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _BLOB_KEYS:
                compact[key_text] = _compact_blob(item)
            else:
                compact[key_text] = _compact_value(item, depth + 1)
        return compact
    return str(value)


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "type": event.get("type"),
        "timestamp": event.get("timestamp") or event.get("ts"),
        "agent_id": event.get("agent_id") or event.get("node_id"),
        "status": event.get("status"),
    }
    for key in ("message", "payload", "sandbox", "error", "reason"):
        if key in event:
            compact[key] = _compact_value(event[key])
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _infer_status(events: list[dict[str, Any]], stored_job: dict[str, Any], run_record: dict[str, Any]) -> str:
    nested_job = stored_job.get("job") if isinstance(stored_job.get("job"), dict) else {}
    status = _first_string(nested_job.get("status"), stored_job.get("status"), run_record.get("status"))
    if status:
        return status
    for event in reversed(events):
        event_type = str(event.get("type") or "").lower()
        event_status = _first_string(event.get("status"), _extract_nested_string(event.get("payload"), "status"))
        if event_status and _is_job_status_event(event_type):
            return event_status
        if "failed" in event_type or "error" in event_type:
            return "failed"
        if "cancel" in event_type:
            return "cancelled"
        if _is_job_completion_event(event_type):
            return "completed"
        if "started" in event_type or "running" in event_type:
            return "running"
    return "unknown"


def _is_job_completion_event(event_type: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")
    return normalized in {
        "job_completed",
        "job_finished",
        "workflow_completed",
        "workflow_finished",
        "blueprint_completed",
        "blueprint_finished",
    }


def _is_job_status_event(event_type: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")
    return normalized.startswith(("job_", "workflow_", "blueprint_"))


def _agent_summaries(stored_job: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents = stored_job.get("agents")
    if isinstance(agents, list) and agents:
        return [_compact_value(agent) for agent in agents if isinstance(agent, dict)]
    counter: Counter[str] = Counter()
    statuses: dict[str, str] = {}
    for event in events:
        agent_id = _first_string(event.get("agent_id"), event.get("node_id"))
        if not agent_id:
            continue
        counter[agent_id] += 1
        statuses[agent_id] = _first_string(event.get("status"), statuses.get(agent_id), "observed")
    return [
        {"agent_id": agent_id, "status": statuses.get(agent_id) or "observed", "event_count": count}
        for agent_id, count in sorted(counter.items())
    ]


def _artifact_content_type(path: Path) -> str:
    return _ARTIFACT_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_id(path: Path, run_dir: Path) -> str:
    rel = path.relative_to(run_dir).as_posix()
    known = {
        "result.json": "result_json",
        "final_artifact.json": "final_artifact_json",
        "events.jsonl": "events_jsonl",
        "logs.jsonl": "logs_jsonl",
        "resources.jsonl": "resources_jsonl",
        "human.jsonl": "human_events_jsonl",
        "job.json": "job_json",
        "run.json": "run_json",
        "ui.json": "ui_json",
        "web_ui.json": "web_ui_json",
    }
    if rel in known:
        return known[rel]
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", rel).strip("_").lower()
    return normalized or "artifact"


def _artifact_ref(run_id: str, path: Path, run_dir: Path) -> dict[str, Any]:
    stat = path.stat()
    rel = path.relative_to(run_dir).as_posix()
    return {
        "artifact_id": _artifact_id(path, run_dir),
        "path": str(path),
        "relative_path": rel,
        "size_bytes": stat.st_size,
        "sha256": _sha256_file(path),
        "content_type": _artifact_content_type(path),
        "url": f"/api/v1/runs/{urllib.parse.quote(run_id)}/artifacts/{urllib.parse.quote(rel, safe='/')}",
    }


def _run_artifacts(run_id: str | None, run_dir: Path | None) -> list[dict[str, Any]]:
    if not run_id or not run_dir or not run_dir.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*"), key=lambda item: item.relative_to(run_dir).as_posix()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in _ARTIFACT_CONTENT_TYPES and path.name not in {"result.json", "final_artifact.json"}:
            continue
        try:
            artifacts.append(_artifact_ref(run_id, path, run_dir))
        except OSError:
            continue
    return artifacts


def _compact_job_detail(job_id: str) -> dict[str, Any]:
    events, stream_error = _stream_job_events(job_id)
    event_run_id = _extract_nested_string(events, "run_id", "runId")
    run_dir = _run_dir_from_id(event_run_id)
    stored_job: dict[str, Any] = {}
    if run_dir is None:
        run_dir, stored_job = _find_run_dir_for_job(job_id)
    else:
        stored_job = _read_json_file(run_dir / "job.json")
    run_record = _read_json_file(run_dir / "run.json") if run_dir else {}
    nested_job = stored_job.get("job") if isinstance(stored_job.get("job"), dict) else {}
    run_id = _first_string(
        nested_job.get("run_id"),
        nested_job.get("runId"),
        stored_job.get("run_id"),
        stored_job.get("runId"),
        run_record.get("run_id"),
        run_record.get("runId"),
        event_run_id,
        run_dir.name if run_dir else "",
    )
    graph_id = _first_string(
        nested_job.get("graph_id"),
        nested_job.get("graphId"),
        stored_job.get("graph_id"),
        stored_job.get("graphId"),
        run_record.get("graph_id"),
        run_record.get("graphId"),
        _extract_nested_string(events, "graph_id", "graphId"),
    )
    status = _infer_status(events, stored_job, run_record)
    artifacts = _run_artifacts(run_id, run_dir)
    recent_events = [_compact_event(event) for event in events[-50:]]
    job = {
        "job_id": job_id,
        "run_id": run_id or None,
        "graph_id": graph_id or None,
        "status": status,
        "run_dir": str(run_dir) if run_dir else None,
        "artifacts": artifacts,
    }
    summary = {
        "mode": "compact",
        "job_id": job_id,
        "run_id": run_id or None,
        "graph_id": graph_id or None,
        "status": status,
        "event_count": len(events),
        "recent_event_count": len(recent_events),
        "artifact_count": len(artifacts),
        "full_detail_url": f"/api/v1/jobs/{urllib.parse.quote(job_id)}?include=full",
    }
    if stream_error:
        summary["event_stream_warning"] = stream_error
    return {
        "job": job,
        "summary": summary,
        "agents": _agent_summaries(stored_job, events),
        "recent_events": recent_events,
        "events": recent_events,
        "artifacts": artifacts,
        "output_files": artifacts,
    }


def _full_job_detail(job_id: str) -> dict[str, Any]:
    try:
        job_json = state.client.get_job(job_id)
        return json.loads(job_json)
    except Exception:
        return _compact_job_detail(job_id)


@router.get("/jobs/{job_id}/agent-graph")
def get_job_agent_graph(job_id: str, _auth=Depends(require_auth)):
    try:
        details = _full_job_detail(job_id)
        events, _stream_error = _stream_job_events(job_id, limit=5000)
        return build_agent_graph(job_id, details, events)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/events")
def get_job_events(
    job_id: str,
    include: str = Query("full"),
    limit: int = Query(200, ge=1, le=5000),
    _auth=Depends(require_auth),
):
    try:
        if include in {"compact", "summary"}:
            events, stream_error = _stream_job_events(job_id, limit=limit)
            response: dict[str, Any] = {"data": [_compact_event(event) for event in events[-limit:]]}
            if stream_error:
                response["warning"] = stream_error
            return response
        if include != "full":
            raise HTTPException(status_code=400, detail="include must be 'full', 'compact', or 'summary'")
        events = []
        for event_json in state.client.stream_events(job_id):
            events.append(json.loads(event_json))
        return {"data": events}
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/dead-letters")
def get_job_dead_letters(job_id: str, _auth=Depends(require_auth)):
    try:
        dead_letters = []
        for event_index, event_json in enumerate(state.client.stream_events(job_id)):
            event = json.loads(event_json)
            if event.get("type") == "dead_letter":
                dead_letters.append(
                    {
                        "index": len(dead_letters),
                        "event_index": event_index,
                        "agent_id": event.get("agent_id"),
                        "reason": event.get("reason") or event.get("error"),
                        "timestamp": event.get("timestamp"),
                        "message": event.get("message"),
                    }
                )
        return {"job_id": job_id, "data": dead_letters}
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/dead-letters/{index}/replay")
def replay_job_dead_letter(job_id: str, index: int, _auth=Depends(require_auth)):
    raise HTTPException(
        status_code=501,
        detail={
            "error": "dead_letter_replay_not_exposed",
            "job_id": job_id,
            "index": index,
            "message": "core replay is available in-process; gRPC replay will be added to expose it over REST",
        },
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _auth=Depends(require_auth)):
    try:
        status = state.client.cancel_job(job_id)
        cleanup_blueprint_processes_for_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as exc:
        cleanup_blueprint_processes_for_job(job_id)
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, _auth=Depends(require_auth)):
    try:
        status = state.client.pause_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, _auth=Depends(require_auth)):
    try:
        status = state.client.resume_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as exc:
        return handle_grpc_error(exc)
