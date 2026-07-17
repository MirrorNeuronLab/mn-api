from __future__ import annotations

import json
import queue
import re
import threading
import time
import urllib.parse
import base64
import asyncio
from collections import Counter, deque
from pathlib import Path
from typing import Any

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from mn_sdk import (
    BlueprintWorkflowProgress,
    ManifestConversionError,
    RuntimeService,
    expand_manifest_source,
    make_validation_report,
    run_hardware_requirements_validation,
    run_input_validation,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
    validate_resource_spec_issues,
    failure_from_event,
    is_manifest_source,
    normalize_error,
    workflow_progress_snapshot,
)
from mn_sdk.blueprint_support.observability import read_run_resources
from mn_sdk.staged_artifacts import (
    ArtifactIntegrityError,
    ArtifactNotReadyError,
    StagedArtifactError,
    is_staged_artifact_ref,
    resolve_json_reference,
)

from mn_api import state
from mn_api.agent_graph import build_agent_graph
from mn_api.artifacts import artifact_ref, list_artifact_files
from mn_api.blueprints import blueprint_bundle_root, cleanup_blueprint_processes_for_job, find_blueprint
from mn_api.blueprints import runtime_resource_report
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth, require_websocket_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
from mn_api.job_activity import compact_event as _compact_event
from mn_api.job_activity import compact_value as _compact_value
from mn_api.job_activity import enrich_workflow_progress_activity as _enrich_workflow_progress_activity
from mn_api.run_outputs import output_refs
from mn_api.run_store import first_string as _first_string
from mn_api.run_store import read_json_file as _read_json_file
from mn_api.run_store import read_jsonl_file as _read_jsonl_file
from mn_api.run_store import run_dir_from_id as _run_dir_from_id
from mn_api.run_store import runs_root as _runs_root
from mn_api.run_store import stream_jsonl_files as _stream_jsonl_files
from mn_api.schemas import RestoreJobBackupRequest, SubmitJobRequest


router = APIRouter(prefix="/api/v1")
_MAX_COMPACT_STRING = 2000
_MAX_COMPACT_LIST = 25
_MAX_STATUS_RUNTIME_EVENTS = 25
_LIST_STATUS_REFRESH_STATUSES = {
    "pending",
    "planned",
    "validated",
    "scheduled",
    "queued",
    "starting",
    "preparing",
    "running",
    "paused",
}
_TERMINAL_EVENT_TYPES = {"job_completed", "job_failed", "job_cancelled"}
_CANCEL_ALL_ACTIVE_STATUSES = {"pending", "validated", "scheduled", "running", "paused"}
_ALL_JOBS_LIMIT = 2_147_483_647
_IMMEDIATE_PROGRESS_EVENTS = {
    "job_pending",
    "job_validated",
    "job_scheduled",
    "job_running",
    "job_pausing",
    "job_paused",
    "job_resumed",
    "workflow_step_started",
    "blueprint_phase_started",
    "workflow_step_completed",
    "blueprint_phase_completed",
    "workflow_step_failed",
    "blueprint_phase_failed",
    "workflow_step_timed_out",
    "workflow_step_attempt_retry_scheduled",
    "workflow_step_attempt_timed_out",
    "workflow_step_blocked",
}


@router.post("/jobs")
def submit_job(req: SubmitJobRequest, _auth=Depends(require_auth)):
    try:
        bundle_dir: str | None = None
        if req.bundle_path:
            manifest_json, payloads_bytes = load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
            bundle_dir = req.bundle_path
            state.close_client()
            validation_response = _validate_job_bundle(req.bundle_path, manifest_json, force=req.force)
            if validation_response is not None:
                return validation_response
        elif req.manifest_json is not None:
            manifest_json = req.manifest_json
            payloads_bytes = {key: value.encode("utf-8") for key, value in req.payloads.items()} if req.payloads else {}
            state.close_client()
            validation_response = _validate_job_manifest(manifest_json, force=req.force)
            if validation_response is not None:
                return validation_response
        else:
            raise HTTPException(
                status_code=422,
                detail="manifest_json or _bundle_path is required",
            )

        return RuntimeService(state.client).submit_job(
            manifest_json,
            payloads_bytes,
            bundle_dir=bundle_dir,
            run_id=_submission_run_id(manifest_json),
            force=req.force,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


def _validate_job_bundle(bundle_path: str, manifest_json: str, *, force: bool):
    manifest = _decode_manifest(manifest_json, root_dir=Path(bundle_path))
    spec_issues = (
        validate_requirements_spec_issues(manifest)
        + validate_resource_spec_issues(manifest)
        + validate_input_validation_spec_issues(manifest)
    )
    if spec_issues:
        return validation_problem_response(
            make_validation_report(spec_issues),
            status_code=422,
            error="manifest_validation_failed",
            title="Manifest validation failed",
            detail="Fix the highlighted manifest fields and submit again.",
        )
    requirements = run_hardware_requirements_validation(
        manifest,
        resource_report=runtime_resource_report,
        force=force,
    )
    if not requirements.get("ok"):
        return validation_problem_response(
            requirements,
            status_code=412,
            error="requirements_not_met",
            title="Runtime node required",
            detail="Add or connect a runtime node that meets this job's hardware requirements, then submit again.",
        )
    if force:
        return None
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
    manifest = _decode_manifest(manifest_json, root_dir=Path.cwd())
    spec_issues = (
        validate_requirements_spec_issues(manifest)
        + validate_resource_spec_issues(manifest)
        + validate_input_validation_spec_issues(manifest)
    )
    if spec_issues:
        return validation_problem_response(
            make_validation_report(spec_issues),
            status_code=422,
            error="manifest_validation_failed",
            title="Manifest validation failed",
            detail="Fix the highlighted manifest fields and submit again.",
        )
    requirements = run_hardware_requirements_validation(
        manifest,
        resource_report=runtime_resource_report,
        force=force,
    )
    if not requirements.get("ok"):
        return validation_problem_response(
            requirements,
            status_code=412,
            error="requirements_not_met",
            title="Runtime node required",
            detail="Add or connect a runtime node that meets this job's hardware requirements, then submit again.",
        )
    if force:
        return None
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


def _decode_manifest(manifest_json: str, *, root_dir: Path | None = None) -> dict:
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="manifest_json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="manifest_json must be an object")
    if is_manifest_source(manifest):
        try:
            manifest = expand_manifest_source(manifest, root_dir=root_dir)
        except ManifestConversionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return manifest


def _submission_run_id(manifest_json: str) -> str | None:
    try:
        manifest = _decode_manifest(manifest_json)
    except Exception:
        return None
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    for value in (
        manifest.get("run_id"),
        metadata.get("blueprint_run_id"),
        metadata.get("run_id"),
        _nested_get(metadata, ["mn_cli", "blueprint_run_id"]),
        _nested_get(metadata, ["mn_cli", "run_id"]),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested_get(value: dict[str, Any], path: list[str]) -> Any:
    cursor: Any = value
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


@router.get("/jobs")
def list_jobs(limit: int = 20, include_terminal: bool = True, _auth=Depends(require_auth)):
    try:
        jobs_json = state.client.list_jobs(limit, include_terminal)
        return _reconcile_job_list_statuses(json.loads(jobs_json))
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs:cleanup", operation_id="cleanup_jobs_colon_alias")
@router.post("/jobs/cleanup", operation_id="cleanup_jobs_path_alias")
def cleanup_jobs(_auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).clear_jobs()
    except Exception as exc:
        if _is_clear_jobs_admin_token_error(exc):
            state.close_client()
            try:
                return RuntimeService(state.client).clear_jobs()
            except Exception as retry_exc:
                return handle_grpc_error(retry_exc)
        return handle_grpc_error(exc)


@router.post("/jobs:cancel-all", operation_id="cancel_all_jobs_colon_alias")
@router.post("/jobs/cancel-all", operation_id="cancel_all_jobs_path_alias")
def cancel_all_jobs(_auth=Depends(require_auth)):
    try:
        jobs_json = state.client.list_jobs(_ALL_JOBS_LIMIT, False)
        payload = json.loads(jobs_json)
        records = payload.get("data") if isinstance(payload, dict) else []
        jobs = [
            job
            for job in records or []
            if isinstance(job, dict)
            and _normalized_status(job.get("status")) in _CANCEL_ALL_ACTIVE_STATUSES
            and isinstance(job.get("job_id"), str)
            and job["job_id"]
        ]
    except Exception as exc:
        return handle_grpc_error(exc)

    cancelled: list[str] = []
    for job in jobs:
        job_id = job["job_id"]
        try:
            state.client.cancel_job(job_id)
            cleanup_blueprint_processes_for_job(job_id)
            cancelled.append(job_id)
        except Exception as exc:
            cleanup_blueprint_processes_for_job(job_id)
            return handle_grpc_error(exc)

    return {
        "version": 1,
        "status": "cancelled" if cancelled else "no_active_jobs",
        "active_count": len(jobs),
        "cancelled_count": len(cancelled),
        "cancelled_job_ids": cancelled,
    }


def _is_clear_jobs_admin_token_error(exc: Exception) -> bool:
    if not isinstance(exc, grpc.RpcError):
        return False
    if exc.code() != grpc.StatusCode.PERMISSION_DENIED:
        return False
    return "MN_GRPC_ADMIN_TOKEN" in str(exc.details())


@router.get("/jobs/unfinished")
def unfinished_jobs(_auth=Depends(require_auth)):
    try:
        jobs_json = state.client.list_jobs(500, False)
        payload = json.loads(jobs_json)
        jobs = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(jobs, list):
            jobs = []
        return {"data": [_unfinished_job_row(job) for job in jobs if isinstance(job, dict)]}
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


@router.get("/jobs/{job_id}/snapshots/{snapshot_kind}")
def get_job_snapshot(job_id: str, snapshot_kind: str, _auth=Depends(require_auth)):
    reference_fields = {
        "result": "result_ref",
        "workflow-state": "workflow_state_ref",
        "workflow_state": "workflow_state_ref",
    }
    reference_field = reference_fields.get(snapshot_kind)
    if reference_field is None:
        raise HTTPException(status_code=404, detail="unknown job snapshot")

    try:
        details = json.loads(state.client.get_job(job_id))
        job = details.get("job") if isinstance(details.get("job"), dict) else details
        reference = job.get(reference_field) if isinstance(job, dict) else None
        if not is_staged_artifact_ref(reference) and isinstance(job, dict):
            result = job.get("result")
            if isinstance(result, dict):
                reference = result.get(reference_field)
        if not is_staged_artifact_ref(reference):
            raise HTTPException(status_code=404, detail=f"{snapshot_kind} snapshot is unavailable")
        return resolve_json_reference(reference)
    except ArtifactNotReadyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "artifact_not_ready", "retryable": True, "message": str(exc)},
            headers={"Retry-After": "1"},
        ) from exc
    except ArtifactIntegrityError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "artifact_integrity_error", "message": str(exc)},
        ) from exc
    except StagedArtifactError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "artifact_resolution_error", "message": str(exc)},
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_success_status(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", _normalized_status(value)).strip("_")
    return normalized in {"completed", "complete", "done", "finished", "succeeded", "success"}


def _clear_success_failure(snapshot: dict[str, Any]) -> None:
    if _is_success_status(snapshot.get("status")):
        snapshot.pop("failure", None)


def _should_reconcile_job_list_status(job: dict[str, Any]) -> bool:
    job_id = _first_string(job.get("job_id"), job.get("id"))
    status = re.sub(r"[^a-z0-9]+", "_", _normalized_status(job.get("status"))).strip("_")
    return bool(job_id and job_id != "unknown" and status in _LIST_STATUS_REFRESH_STATUSES)


def _status_from_workflow_progress(snapshot: dict[str, Any]) -> str:
    status = _normalized_status(snapshot.get("status"))
    return status if status and status != "unknown" else ""


def _reconciled_job_list_row(job: dict[str, Any]) -> dict[str, Any]:
    if not _should_reconcile_job_list_status(job):
        return job
    job_id = _first_string(job.get("job_id"), job.get("id"))
    if not job_id:
        return job
    try:
        status = _status_from_workflow_progress(_workflow_progress_snapshot_for_job(job_id))
    except Exception:
        return job
    return {**job, "status": status} if status else job


def _reconcile_job_list_statuses(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    jobs = payload.get("data")
    if not isinstance(jobs, list):
        return payload
    return {
        **payload,
        "data": [_reconciled_job_list_row(job) if isinstance(job, dict) else job for job in jobs],
    }


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
        for event_json in state.client.stream_events(job_id, follow=False, limit=limit):
            try:
                event = json.loads(event_json)
            except json.JSONDecodeError:
                event = {"type": "unparseable_event", "message": str(event_json)[:_MAX_COMPACT_STRING]}
            if isinstance(event, dict):
                events.append(event)
    except Exception as exc:
        return list(events), str(exc)
    return list(events), None


def _run_dir_for_events(job_id: str, events: list[dict[str, Any]]) -> Path | None:
    run_dir = _run_dir_from_id(_extract_nested_string(events, "run_id", "runId"))
    if run_dir is not None:
        return run_dir
    found_run_dir, _stored_job = _find_run_dir_for_job(job_id)
    return found_run_dir


def _merged_job_events(job_id: str, *, limit: int = 5000) -> tuple[list[dict[str, Any]], str | None]:
    events, stream_error = _stream_job_events(job_id, limit=limit)
    run_dir = _run_dir_for_events(job_id, events)
    if run_dir is not None:
        events = _merge_events(events, _run_store_events(run_dir, limit=limit), limit=limit)
    return events, stream_error


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


def _infer_status(events: list[dict[str, Any]], stored_job: dict[str, Any], run_record: dict[str, Any]) -> str:
    nested_job = stored_job.get("job") if isinstance(stored_job.get("job"), dict) else {}
    status = _first_string(nested_job.get("status"), stored_job.get("status"), run_record.get("status"))
    if status:
        return status
    for event in reversed(events):
        event_type = str(event.get("type") or "").lower()
        normalized_event_type = re.sub(r"[^a-z0-9]+", "_", event_type).strip("_")
        event_status = _first_string(event.get("status"), _extract_nested_string(event.get("payload"), "status"))
        if event_status and _is_job_status_event(event_type):
            return event_status
        lifecycle_status = {
            "job_pausing": "pausing",
            "job_paused": "paused",
            "job_resumed": "running",
            "job_cancelled": "cancelled",
        }.get(normalized_event_type)
        if lifecycle_status:
            return lifecycle_status
        if "failed" in event_type or "error" in event_type:
            return "failed"
        if "cancel" in event_type:
            return "cancelled"
        if _is_job_completion_event(event_type):
            return "completed"
        if "started" in event_type or "running" in event_type:
            return "running"
    return "unknown"


def _failure_from_sources(
    events: list[dict[str, Any]],
    stored_job: dict[str, Any],
    run_record: dict[str, Any],
) -> dict[str, Any] | None:
    for event in reversed(events):
        failure = failure_from_event(event)
        if failure:
            return failure

    for mapping in (
        stored_job,
        stored_job.get("job") if isinstance(stored_job.get("job"), dict) else {},
        run_record,
    ):
        failure = _failure_from_mapping(mapping)
        if failure:
            return failure
    return None


def _failure_from_mapping(mapping: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(mapping, dict):
        return None
    for key in ("failure", "error"):
        value = mapping.get(key)
        if value not in (None, ""):
            return normalize_error(value, context={"code": "runtime.job.failed"})
    result = mapping.get("result")
    if isinstance(result, dict):
        for key in ("failure", "error", "reason", "status_reason"):
            value = result.get(key)
            if value not in (None, ""):
                return normalize_error(value, context={"code": "runtime.job.failed"})
    reason = _first_string(mapping.get("reason"), mapping.get("status_reason"))
    if reason:
        return normalize_error(reason, context={"code": "runtime.job.failed"})
    return None


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


def _run_artifacts(run_id: str | None, run_dir: Path | None) -> list[dict[str, Any]]:
    if not run_id or not run_dir or not run_dir.exists():
        return []
    artifacts: list[dict[str, Any]] = []
    for path in list_artifact_files(run_dir):
        try:
            artifacts.append(artifact_ref(run_id, path, run_dir))
        except OSError:
            continue
    seen_paths = {artifact.get("path") for artifact in artifacts}
    for artifact in output_refs(run_id, run_dir):
        if artifact.get("path") in seen_paths:
            continue
        artifacts.append(artifact)
        seen_paths.add(artifact.get("path"))
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
    observability_summary = _read_json_file(run_dir / "observability_summary.json") if run_dir else {}
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
    failure = None if _is_success_status(status) else _failure_from_sources(events, stored_job, run_record)
    trace_id = _first_string(
        run_record.get("trace_id"),
        observability_summary.get("trace_id"),
        _extract_nested_string(events, "trace_id", "traceId"),
    )
    job = {
        "job_id": job_id,
        "run_id": run_id or None,
        "trace_id": trace_id or None,
        "graph_id": graph_id or None,
        "status": status,
        "run_dir": str(run_dir) if run_dir else None,
        "artifacts": artifacts,
    }
    if failure:
        job["failure"] = failure
    summary = {
        "mode": "compact",
        "job_id": job_id,
        "run_id": run_id or None,
        "trace_id": trace_id or None,
        "graph_id": graph_id or None,
        "status": status,
        "event_count": len(events),
        "recent_event_count": len(recent_events),
        "artifact_count": len(artifacts),
        "full_detail_url": f"/api/v1/jobs/{urllib.parse.quote(job_id)}?include=full",
    }
    if failure:
        summary["failure"] = failure
    if observability_summary:
        summary["observability_summary"] = _compact_value(observability_summary)
    resource_usage = _read_run_resource_usage(run_id) if run_id else None
    if resource_usage:
        summary["resource_usage"] = _compact_value(resource_usage)
    if stream_error:
        summary["event_stream_warning"] = stream_error
    return {
        "job": job,
        "summary": summary,
        "trace_id": trace_id or None,
        "observability_summary": observability_summary,
        "resource_usage": resource_usage,
        "failure": failure,
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


def _read_run_resource_usage(run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    try:
        return read_run_resources(run_id, runs_root=_runs_root())
    except Exception:
        return None


def _workflow_progress_snapshot_for_job(job_id: str) -> dict[str, Any]:
    details = _full_job_detail(job_id)
    job = _job_from_details(details)
    summary = _summary_from_details(details)
    events, stream_error = _stream_job_events(job_id, limit=_MAX_STATUS_RUNTIME_EVENTS)
    run_dir = _run_dir_for_details(details, events, job_id=job_id)
    events = _merge_events(
        events,
        _run_store_events(run_dir, limit=_MAX_STATUS_RUNTIME_EVENTS),
        limit=_MAX_STATUS_RUNTIME_EVENTS,
    )
    observability_summary = _read_json_file(run_dir / "observability_summary.json") if run_dir else {}
    manifest = _manifest_with_public_agent_bindings(
        _manifest_from_job_details(details, run_dir=run_dir),
        job,
        summary,
    )
    snapshot = workflow_progress_snapshot(
        manifest,
        events,
        job=job,
        summary=summary,
        job_id=job_id,
    )
    _apply_default_assigned_node(snapshot, details)
    _enrich_workflow_progress_activity(snapshot, events)
    _clear_success_failure(snapshot)
    if not snapshot.get("failure") and not _is_success_status(snapshot.get("status")):
        failure = _failure_from_sources(events, job, summary)
        if failure:
            snapshot["failure"] = failure
    if stream_error:
        snapshot["warning"] = stream_error
    trace_id = _first_string(
        snapshot.get("trace_id"),
        observability_summary.get("trace_id"),
        (_read_json_file(run_dir / "run.json") if run_dir else {}).get("trace_id"),
        _extract_nested_string(events, "trace_id", "traceId"),
    )
    if trace_id:
        snapshot["trace_id"] = trace_id
    if observability_summary:
        snapshot["observability_summary"] = observability_summary
    return snapshot


def _job_from_details(details: dict[str, Any]) -> dict[str, Any]:
    job = details.get("job") if isinstance(details.get("job"), dict) else {}
    return job


def _summary_from_details(details: dict[str, Any]) -> dict[str, Any]:
    summary = details.get("summary") if isinstance(details.get("summary"), dict) else {}
    return summary


def _manifest_from_job_details(details: dict[str, Any], *, run_dir: Path | None = None) -> dict[str, Any]:
    job = _job_from_details(details)
    summary = _summary_from_details(details)
    public_manifest = _public_workflow_manifest_from_job(job, summary)
    run_manifest = _manifest_from_run_dir(run_dir)
    blueprint_manifest = _blueprint_manifest_from_run_mapping(run_dir)
    for candidate in (run_manifest, blueprint_manifest):
        if _matches_public_workflow_contract(candidate, public_manifest):
            return candidate

    direct_manifests = (
        details.get("manifest"),
        job.get("manifest"),
        summary.get("manifest"),
    )
    for candidate in direct_manifests:
        if _matches_public_workflow_contract(candidate, public_manifest):
            return candidate

    manifest_ref = job.get("manifest_ref")
    if not isinstance(manifest_ref, dict):
        manifest_ref = summary.get("manifest_ref")
    ref_manifest: dict[str, Any] = {}
    if isinstance(manifest_ref, dict):
        for raw_path in (
            manifest_ref.get("manifest_path"),
            Path(str(manifest_ref.get("job_path") or "")) / "manifest.json" if manifest_ref.get("job_path") else None,
        ):
            if not raw_path:
                continue
            try:
                ref_manifest = _read_workflow_manifest(Path(str(raw_path)).expanduser())
                if ref_manifest:
                    break
            except OSError:
                continue
    if _matches_public_workflow_contract(ref_manifest, public_manifest):
        return ref_manifest

    if public_manifest:
        return public_manifest

    for candidate in (run_manifest, blueprint_manifest, *direct_manifests, ref_manifest):
        if isinstance(candidate, dict) and candidate:
            return candidate

    return _fallback_manifest_from_details(details)


def _manifest_has_workflow_flow(manifest: dict[str, Any]) -> bool:
    workflow = manifest.get("workflow") if isinstance(manifest, dict) else None
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    return isinstance(steps, list) and bool(steps)


def _manifest_from_run_dir(run_dir: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return {}
    for filename in ("manifest.json", "config.json"):
        manifest = _read_workflow_manifest(run_dir / filename)
        if manifest:
            return manifest
    return {}


def _read_workflow_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json_file(path)
    if not manifest:
        return {}
    try:
        if is_manifest_source(manifest):
            manifest = expand_manifest_source(manifest, root_dir=path.parent)
    except Exception:
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _blueprint_manifest_from_run_mapping(run_dir: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return {}
    mapping = _read_json_file(run_dir / "job.json")
    if not mapping:
        return {}

    for key in ("blueprint_path", "blueprint_source"):
        raw_path = _first_string(mapping.get(key))
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if path.is_dir():
            path = path / "manifest.json"
        manifest = _read_workflow_manifest(path)
        if manifest:
            return manifest

    blueprint_id = _first_string(mapping.get("blueprint_id"))
    if not blueprint_id:
        return {}
    try:
        repo_root, blueprint = find_blueprint(state.refresh_config_from_env(), blueprint_id)
        return _read_workflow_manifest(blueprint_bundle_root(repo_root, blueprint) / "manifest.json")
    except Exception:
        return {}


def _matches_public_workflow_contract(candidate: Any, public_manifest: dict[str, Any] | None) -> bool:
    if not isinstance(candidate, dict) or not candidate or not _manifest_has_workflow_flow(candidate):
        return False
    if public_manifest is None:
        return True
    return _workflow_step_ids(candidate) == _workflow_step_ids(public_manifest)


def _workflow_step_ids(manifest: dict[str, Any]) -> list[str]:
    workflow = manifest.get("workflow") if isinstance(manifest.get("workflow"), dict) else {}
    steps = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
    return [str(step.get("id")) for step in steps if isinstance(step, dict) and step.get("id")]


def _public_workflow_manifest_from_job(
    job: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any] | None:
    """Rebuild the source-facing workflow contract from the runtime ledger."""

    workflow_state = _workflow_state_from_job(job, summary)
    steps_by_id = (
        workflow_state.get("steps")
        if isinstance(workflow_state, dict) and isinstance(workflow_state.get("steps"), dict)
        else {}
    )
    step_order = (
        workflow_state.get("step_order")
        if isinstance(workflow_state, dict) and isinstance(workflow_state.get("step_order"), list)
        else []
    )
    step_ids = [str(step_id) for step_id in step_order if str(step_id) in steps_by_id]
    if not step_ids:
        step_ids = [str(step_id) for step_id in steps_by_id if str(step_id).strip()]
    if not step_ids:
        step_ids = _public_step_ids_from_topology(job)
    if not step_ids:
        return None

    edges = _public_workflow_edges(workflow_state, job, step_ids)
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        event_name = str(edge.get("event") or edge.get("message_type") or "")
        if source and target and event_name:
            outgoing.setdefault(source, []).append((event_name, target))

    steps: list[dict[str, Any]] = []
    for index, step_id in enumerate(step_ids):
        record = steps_by_id.get(step_id)
        record = record if isinstance(record, dict) else {}
        transitions = {event_name: target for event_name, target in outgoing.get(step_id, [])}
        steps.append(
            {
                "id": step_id,
                "label": str(record.get("label") or _humanize_identifier(step_id)),
                "goal": str(record.get("goal") or ""),
                "run": str(record.get("run") or f"{step_id}__start"),
                "emits": _step_emit_name(step_id, outgoing.get(step_id, [])),
                "on": transitions,
                "needs": [
                    str(edge.get("from"))
                    for edge in edges
                    if str(edge.get("to") or "") == step_id
                ],
                "kind": "source" if index == 0 else "sink" if index == len(step_ids) - 1 else "stage",
            }
        )

    workflow_id = str(
        workflow_state.get("workflow_id")
        if isinstance(workflow_state, dict) and workflow_state.get("workflow_id")
        else job.get("workflow_id")
        or summary.get("workflow_id")
        or job.get("graph_id")
        or summary.get("graph_id")
        or job.get("job_id")
        or "workflow"
    )
    job_type = str(job.get("job_type") or job.get("type") or summary.get("job_type") or summary.get("type") or "")
    return {
        "apiVersion": "mn.workflow/v1",
        "kind": "Workflow",
        "id": str(job.get("graph_id") or summary.get("graph_id") or workflow_id),
        "name": str(job.get("job_name") or summary.get("job_name") or workflow_id),
        "description": str(summary.get("description") or job.get("description") or ""),
        "policies": {"stream_mode": "live"} if job_type.lower() == "service" else {},
        "workflow": {
            "workflow_id": workflow_id,
            "entrypoint": step_ids[0],
            "source": step_ids[0],
            "sink": step_ids[-1],
            "steps": steps,
            "edges": edges,
        },
        "runtime": {"bindings": {}},
    }


def _workflow_state_from_job(job: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any] | None:
    for mapping in (job, summary):
        workflow_state = mapping.get("workflow_state") if isinstance(mapping, dict) else None
        if isinstance(workflow_state, dict):
            return workflow_state
    return None


def _public_step_ids_from_topology(job: dict[str, Any]) -> list[str]:
    topology = job.get("runtime_topology") if isinstance(job.get("runtime_topology"), dict) else {}
    nodes = topology.get("nodes") if isinstance(topology.get("nodes"), list) else []
    step_ids: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("node_id") or node.get("id") or "")
        node_types = {str(node.get(key) or "").strip().lower() for key in ("agent_type", "node_type", "type")}
        if "step_source" not in node_types or not node_id.endswith("__start"):
            continue
        step_id = node_id.removesuffix("__start")
        if step_id and step_id not in step_ids:
            step_ids.append(step_id)
    return step_ids


def _public_workflow_edges(
    workflow_state: dict[str, Any] | None,
    job: dict[str, Any],
    step_ids: list[str],
) -> list[dict[str, Any]]:
    raw_edges = workflow_state.get("edges") if isinstance(workflow_state, dict) else None
    if not isinstance(raw_edges, list):
        topology = job.get("runtime_topology") if isinstance(job.get("runtime_topology"), dict) else {}
        raw_edges = topology.get("edges") if isinstance(topology.get("edges"), list) else []
    known_steps = set(step_ids)
    edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        source = str(raw_edge.get("from") or raw_edge.get("from_node") or "")
        target = str(raw_edge.get("to") or raw_edge.get("to_node") or "")
        if source.endswith("__end"):
            source = source.removesuffix("__end")
        if target.endswith("__start"):
            target = target.removesuffix("__start")
        if source not in known_steps or target not in known_steps:
            continue
        edges.append(
            {
                "id": str(raw_edge.get("id") or raw_edge.get("edge_id") or f"{source}_to_{target}"),
                "from": source,
                "to": target,
                "event": str(raw_edge.get("event") or raw_edge.get("message_type") or f"{source}_completed"),
            }
        )
    return edges


def _step_emit_name(step_id: str, transitions: list[tuple[str, str]]) -> str:
    return transitions[0][0] if transitions else f"{step_id}_completed"


def _humanize_identifier(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("-", "_").split("_") if part)


def _manifest_with_public_agent_bindings(
    manifest: dict[str, Any],
    job: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Bind public steps to ledger agent IDs instead of lowered runtime nodes."""

    workflow = manifest.get("workflow") if isinstance(manifest.get("workflow"), dict) else {}
    raw_steps = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    raw_bindings = runtime.get("bindings") if isinstance(runtime.get("bindings"), dict) else {}
    workflow_state = _workflow_state_from_job(job, summary) or {}
    ledger_steps = workflow_state.get("steps") if isinstance(workflow_state.get("steps"), dict) else {}
    if not raw_steps or not raw_bindings:
        return manifest

    bindings = dict(raw_bindings)
    changed = False
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            continue
        step_id = str(raw_step.get("id") or "")
        run_id = str(raw_step.get("run") or step_id)
        binding = bindings.get(step_id) or bindings.get(run_id)
        if not step_id or not isinstance(binding, dict):
            continue

        ledger_record = ledger_steps.get(step_id)
        ledger_record = ledger_record if isinstance(ledger_record, dict) else {}
        ledger_agent_ids = [
            public_id
            for agent_id in ledger_record.get("agent_ids", [])
            if (public_id := _public_agent_id(step_id, agent_id))
        ] if isinstance(ledger_record.get("agent_ids"), list) else []
        normalized_binding = dict(binding)
        if ledger_agent_ids:
            workers = binding.get("workers")
            if not isinstance(workers, list) or not workers:
                workers = [binding.get("worker") or binding]
            normalized_workers: list[dict[str, Any]] = []
            for index, agent_id in enumerate(ledger_agent_ids):
                matching = next(
                    (
                        worker
                        for worker in workers
                        if isinstance(worker, dict)
                        and _public_agent_id(step_id, worker.get("id") or worker.get("node_id")) == agent_id
                    ),
                    workers[index] if index < len(workers) else {},
                )
                worker = dict(matching) if isinstance(matching, dict) else {}
                worker["id"] = agent_id
                normalized_workers.append(worker)
            normalized_binding["workers"] = normalized_workers
            normalized_binding.pop("worker", None)

        if bindings.get(step_id) is not normalized_binding:
            bindings[step_id] = normalized_binding
            changed = True
        if run_id and bindings.get(run_id) is not normalized_binding:
            bindings[run_id] = normalized_binding
            changed = True

    if not changed:
        return manifest
    return {**manifest, "runtime": {**runtime, "bindings": bindings}}


def _public_agent_id(step_id: str, value: Any) -> str:
    agent_id = str(value or "").strip()
    if not agent_id:
        return ""
    public_id = agent_id.removeprefix(f"{step_id}__")
    if public_id in {"start", "end"} or re.fullmatch(r"(?:fork|join)(?:_\d+)?", public_id):
        return ""
    return public_id


def _run_dir_for_details(
    details: dict[str, Any], events: list[dict[str, Any]], *, job_id: str | None = None
) -> Path | None:
    job = _job_from_details(details)
    summary = _summary_from_details(details)
    run_id = _first_string(
        job.get("run_id"),
        job.get("runId"),
        summary.get("run_id"),
        summary.get("runId"),
        details.get("run_id"),
        details.get("runId"),
        _extract_nested_string(events, "run_id", "runId"),
    )
    run_dir = _run_dir_from_id(run_id)
    if run_dir is not None:
        return run_dir
    if job_id:
        found_run_dir, _stored_job = _find_run_dir_for_job(job_id)
        return found_run_dir
    return None


def _run_store_events(run_dir: Path | None, *, limit: int = 5000) -> list[dict[str, Any]]:
    if run_dir is None:
        return []
    records: list[dict[str, Any]] = []
    for path in _stream_jsonl_files(run_dir, "events.jsonl"):
        records.extend(_read_jsonl_file(path, limit=limit))
        if limit is not None and limit >= 0 and len(records) > limit:
            records = records[-limit:]
    return records[-limit:] if limit is not None and limit >= 0 else records


def _merge_events(*event_groups: list[dict[str, Any]], limit: int = 5000) -> list[dict[str, Any]]:
    merged: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    index = 0
    for events in event_groups:
        for event in events or []:
            if not isinstance(event, dict):
                continue
            key = _event_key(event)
            if key in seen:
                continue
            seen.add(key)
            merged.append((index, event))
            index += 1
    merged.sort(key=lambda item: (str(item[1].get("timestamp") or item[1].get("ts") or ""), item[0]))
    return [event for _index, event in merged[-limit:]]


def _apply_default_assigned_node(snapshot: dict[str, Any], details: dict[str, Any]) -> None:
    assigned_node = _default_assigned_node(details) or "workflow/runtime"
    for step in snapshot.get("steps") or []:
        _assign_step_agents(step, assigned_node)
    current_step = snapshot.get("current_step")
    if isinstance(current_step, dict):
        _assign_step_agents(current_step, assigned_node)


def _assign_step_agents(step: dict[str, Any], assigned_node: str) -> None:
    agents = step.get("agents")
    if not isinstance(agents, list):
        return
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if not _known_assigned_node(agent.get("assigned_node")):
            agent["assigned_node"] = assigned_node


def _default_assigned_node(details: dict[str, Any]) -> str:
    records: list[dict[str, Any]] = []
    agents = details.get("agents")
    if isinstance(agents, list):
        records.extend(agent for agent in agents if isinstance(agent, dict))
    topology = _job_from_details(details).get("runtime_topology")
    if isinstance(topology, dict) and isinstance(topology.get("nodes"), list):
        records.extend(node for node in topology["nodes"] if isinstance(node, dict))
    for record in records:
        value = _first_string(record.get("assigned_node"), record.get("node"))
        if _known_assigned_node(value):
            return value
    return ""


def _known_assigned_node(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() not in {"", "unknown", "unassigned"}


def _fallback_manifest_from_details(details: dict[str, Any]) -> dict[str, Any]:
    job = _job_from_details(details)
    summary = _summary_from_details(details)
    topology = job.get("runtime_topology") if isinstance(job.get("runtime_topology"), dict) else {}
    topology_nodes = topology.get("nodes") if isinstance(topology.get("nodes"), list) else []
    agents = topology_nodes or (details.get("agents") if isinstance(details.get("agents"), list) else [])
    nodes = []
    for index, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue
        agent_id = _first_string(agent.get("agent_id"), agent.get("id"), agent.get("node_id")) or f"agent_{index + 1}"
        nodes.append(
            {
                "node_id": agent_id,
                "alias": _first_string(agent.get("alias")),
                "display_name": _first_string(agent.get("display_name"), agent.get("label")),
                "agent_type": _first_string(agent.get("agent_type"), agent.get("type"), "worker"),
                "role": _first_string(agent.get("role"), agent.get("current_task"), agent.get("agent_type"), "worker"),
                "type": _first_string(agent.get("node_type"), agent.get("type")),
                "live": agent.get("live?") if "live?" in agent else agent.get("live"),
                "config": {
                    "llm_config": _first_string(agent.get("model"), agent.get("llm_config"), "runtime"),
                },
            }
        )
    job_type = _first_string(job.get("job_type"), job.get("type"), summary.get("job_type"), summary.get("type"))
    policies: dict[str, Any] = {}
    if str(job_type or "").lower() == "service":
        policies["stream_mode"] = "live"
    return {
        "id": _first_string(job.get("graph_id"), summary.get("graph_id"), job.get("job_id"), "job"),
        "name": _first_string(job.get("job_name"), summary.get("job_name"), job.get("job_id"), "Job"),
        "description": _first_string(summary.get("description"), job.get("description")),
        "graph_id": _first_string(job.get("graph_id"), summary.get("graph_id")),
        "type": job_type,
        "job_type": job_type,
        "policies": policies,
        "nodes": nodes,
    }


def _event_key(event: dict[str, Any]) -> str:
    try:
        return json.dumps(event, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(event)


def _sse_event(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, sort_keys=True)}\n\n"


def _sse_heartbeat(job_id: str) -> str:
    return _sse_event("heartbeat", {"job_id": job_id})


def _progress_event_should_flush(event_type: str) -> bool:
    normalized = str(event_type or "").strip().lower()
    if normalized in _TERMINAL_EVENT_TYPES or normalized in _IMMEDIATE_PROGRESS_EVENTS:
        return True
    return "failed" in normalized or "error" in normalized or "timed_out" in normalized


def _unfinished_job_row(job: dict[str, Any]) -> dict[str, Any]:
    recovery = job.get("recovery") if isinstance(job.get("recovery"), dict) else {}
    row = dict(job)
    row["recovery_status"] = job.get("recovery_status") or recovery.get("status") or "normal"
    row["recovery_requires_review"] = bool(job.get("recovery_requires_review") or recovery.get("requires_review"))
    return row


def _decode_event_payload(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {"type": "unparseable_event", "message": payload[:_MAX_COMPACT_STRING]}
    return decoded if isinstance(decoded, dict) else {"type": "event", "payload": decoded}


@router.get("/jobs/{job_id}/agent-graph")
def get_job_agent_graph(job_id: str, _auth=Depends(require_auth)):
    try:
        details = _full_job_detail(job_id)
        events, _stream_error = _stream_job_events(job_id, limit=_MAX_STATUS_RUNTIME_EVENTS)
        run_dir = _run_dir_for_details(details, events, job_id=job_id)
        events = _merge_events(
            events,
            _run_store_events(run_dir, limit=_MAX_STATUS_RUNTIME_EVENTS),
            limit=_MAX_STATUS_RUNTIME_EVENTS,
        )
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
            stream_limit = min(limit, _MAX_STATUS_RUNTIME_EVENTS)
            events, stream_error = _merged_job_events(job_id, limit=stream_limit)
            response: dict[str, Any] = {"data": [_compact_event(event) for event in events[-stream_limit:]]}
            if stream_error:
                response["warning"] = stream_error
            return response
        if include != "full":
            raise HTTPException(status_code=400, detail="include must be 'full', 'compact', or 'summary'")
        events, stream_error = _merged_job_events(job_id, limit=limit)
        response: dict[str, Any] = {"data": events[-limit:]}
        if stream_error:
            response["warning"] = stream_error
        return response
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/workflow-progress")
def get_job_workflow_progress(job_id: str, _auth=Depends(require_auth)):
    try:
        return _workflow_progress_snapshot_for_job(job_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/workflow-progress/stream")
def stream_job_workflow_progress(
    job_id: str,
    interval: float = Query(1.0, ge=0.25, le=30.0),
    _auth=Depends(require_auth),
):
    def event_source():
        stop = threading.Event()
        event_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        details = _full_job_detail(job_id)
        job = _job_from_details(details)
        summary = _summary_from_details(details)
        events, stream_error = _stream_job_events(job_id, limit=_MAX_STATUS_RUNTIME_EVENTS)
        run_dir = _run_dir_for_details(details, events, job_id=job_id)
        manifest = _manifest_with_public_agent_bindings(
            _manifest_from_job_details(details, run_dir=run_dir),
            job,
            summary,
        )
        events = _merge_events(
            events,
            _run_store_events(run_dir, limit=_MAX_STATUS_RUNTIME_EVENTS),
            limit=_MAX_STATUS_RUNTIME_EVENTS,
        )
        activity_events = list(events)
        observability_summary = _read_json_file(run_dir / "observability_summary.json") if run_dir else {}
        tracker = BlueprintWorkflowProgress(
            manifest,
            job_id=job_id,
            job=job,
            summary=summary,
        )
        seen = set()
        for event in events:
            seen.add(_event_key(event))
            tracker.update(event)
        tracker.apply_workflow_state(_workflow_state_from_job(job, summary))
        tracker.apply_job_status(job, summary)
        initial = tracker.snapshot(job=job, summary=summary)
        _apply_default_assigned_node(initial, details)
        _enrich_workflow_progress_activity(initial, activity_events)
        _clear_success_failure(initial)
        trace_id = _first_string(
            initial.get("trace_id"),
            observability_summary.get("trace_id"),
            (_read_json_file(run_dir / "run.json") if run_dir else {}).get("trace_id"),
            _extract_nested_string(events, "trace_id", "traceId"),
        )
        if trace_id:
            initial["trace_id"] = trace_id
        if observability_summary:
            initial["observability_summary"] = observability_summary
        if stream_error:
            initial["warning"] = stream_error
        yield _sse_event("snapshot", initial)
        last_sent_at = time.monotonic()
        emit_interval = max(float(interval), 0.5)
        heartbeat_interval = max(float(interval), 5.0)
        next_heartbeat_at = last_sent_at + heartbeat_interval
        pending_snapshot: dict[str, Any] | None = None

        def pump_events() -> None:
            try:
                for event_json in state.client.stream_events(
                    job_id,
                    follow=True,
                    timeout=None,
                    heartbeat_interval_ms=max(int(interval * 1000), 250),
                ):
                    if stop.is_set():
                        break
                    event_queue.put(("event", event_json))
            except Exception as exc:  # pragma: no cover - exercised through API-level fallback behavior.
                event_queue.put(("error", str(exc)))
            finally:
                event_queue.put(("done", None))

        worker = threading.Thread(target=pump_events, daemon=True)
        worker.start()
        try:
            while True:
                try:
                    kind, payload = event_queue.get(timeout=emit_interval)
                except queue.Empty:
                    now = time.monotonic()
                    if pending_snapshot is not None and now - last_sent_at >= emit_interval:
                        yield _sse_event("snapshot", pending_snapshot)
                        pending_snapshot = None
                        last_sent_at = now
                        next_heartbeat_at = now + heartbeat_interval
                    elif now >= next_heartbeat_at:
                        yield _sse_heartbeat(job_id)
                        next_heartbeat_at = now + heartbeat_interval
                    continue

                if kind == "done":
                    break
                if kind == "error":
                    yield _sse_event("error", {"job_id": job_id, "error": payload or "event stream failed"})
                    break
                if not payload:
                    continue

                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    event = {"type": "unparseable_event", "message": payload[:_MAX_COMPACT_STRING]}
                if not isinstance(event, dict):
                    continue

                event_type = str(event.get("type") or "")
                if event_type == "stream_heartbeat":
                    now = time.monotonic()
                    if now >= next_heartbeat_at:
                        yield _sse_heartbeat(job_id)
                        next_heartbeat_at = now + heartbeat_interval
                    continue
                event_key = _event_key(event)
                if event_key in seen:
                    continue
                seen.add(event_key)
                if len(seen) > 10_000:
                    seen.clear()

                activity_events = _merge_events(activity_events, [event], limit=_MAX_STATUS_RUNTIME_EVENTS)
                tracker.update(event)
                if event_type in {"job_completed", "job_failed", "job_cancelled"}:
                    details = _full_job_detail(job_id)
                    job = _job_from_details(details)
                    summary = _summary_from_details(details)
                    tracker.apply_workflow_state(_workflow_state_from_job(job, summary))
                    tracker.apply_job_status(job, summary)
                snapshot = tracker.snapshot(job=job, summary=summary)
                _apply_default_assigned_node(snapshot, details)
                _enrich_workflow_progress_activity(snapshot, activity_events)
                _clear_success_failure(snapshot)
                now = time.monotonic()
                immediate = _progress_event_should_flush(event_type)
                if immediate or now - last_sent_at >= emit_interval:
                    yield _sse_event("snapshot", snapshot)
                    pending_snapshot = None
                    last_sent_at = now
                    next_heartbeat_at = now + heartbeat_interval
                else:
                    pending_snapshot = snapshot
                if event_type in _TERMINAL_EVENT_TYPES:
                    break
        finally:
            stop.set()

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.websocket("/jobs/{job_id}/workflow-progress/ws")
async def websocket_job_workflow_progress(
    websocket: WebSocket,
    job_id: str,
    interval: float = Query(1.0, ge=0.25, le=30.0),
):
    await require_websocket_auth(websocket)
    await websocket.accept()
    stop = threading.Event()
    try:
        await websocket.send_json({"event": "snapshot", "data": _workflow_progress_snapshot_for_job(job_id)})
        event_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def pump_events() -> None:
            try:
                for event_json in state.client.stream_events(
                    job_id,
                    follow=True,
                    timeout=None,
                    heartbeat_interval_ms=max(int(interval * 1000), 250),
                ):
                    if stop.is_set():
                        break
                    event_queue.put(("event", event_json))
            except Exception as exc:
                event_queue.put(("error", str(exc)))
            finally:
                event_queue.put(("done", None))

        threading.Thread(target=pump_events, daemon=True).start()
        heartbeat_seconds = max(float(interval), 5.0)
        last_heartbeat = time.monotonic()
        while True:
            try:
                kind, payload = event_queue.get_nowait()
            except queue.Empty:
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_seconds:
                    await websocket.send_json({"event": "heartbeat", "data": {"job_id": job_id}})
                    last_heartbeat = now
                await asyncio.sleep(min(max(float(interval), 0.25), 1.0))
                continue

            if kind == "done":
                await websocket.close(code=1000)
                break
            if kind == "error":
                await websocket.send_json(
                    {"event": "error", "data": {"job_id": job_id, "error": payload or "event stream failed"}}
                )
                await websocket.close(code=1011)
                break

            event = _decode_event_payload(payload)
            event_type = str(event.get("type") or "")
            if event_type == "stream_heartbeat":
                await websocket.send_json({"event": "heartbeat", "data": {"job_id": job_id}})
                last_heartbeat = time.monotonic()
                continue
            await websocket.send_json({"event": "event", "data": event})
            await websocket.send_json({"event": "snapshot", "data": _workflow_progress_snapshot_for_job(job_id)})
            if event_type in _TERMINAL_EVENT_TYPES:
                await websocket.close(code=1000)
                break
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()


@router.get("/jobs/{job_id}/dead-letters")
def get_job_dead_letters(job_id: str, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).dead_letters(job_id)
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
        result = RuntimeService(state.client).cancel_job(job_id)
        cleanup_blueprint_processes_for_job(job_id)
        return result
    except Exception as exc:
        cleanup_blueprint_processes_for_job(job_id)
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/backup")
def export_job_backup(job_id: str, _auth=Depends(require_auth)):
    try:
        backup_json, bundle_files = state.client.export_job_backup(job_id)
        return {
            "job_id": job_id,
            "backup_json": backup_json,
            "bundle_files": {path: base64.b64encode(content).decode("ascii") for path, content in bundle_files.items()},
            "encoding": "base64",
        }
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/restore")
def restore_job_backup(req: RestoreJobBackupRequest, _auth=Depends(require_auth)):
    try:
        bundle_files = {path: base64.b64decode(content.encode("ascii")) for path, content in req.bundle_files.items()}
        return json.loads(
            state.client.restore_job_backup(
                req.backup_json,
                bundle_files,
                blueprint_id=req.blueprint_id,
                run_id=req.run_id,
            )
        )
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).pause_job(job_id)
    except Exception as exc:
        legacy = _legacy_job_control_error(exc)
        if legacy is not None:
            return legacy
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: str, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).resume_job(job_id)
    except Exception as exc:
        legacy = _legacy_job_control_error(exc)
        if legacy is not None:
            return legacy
        return handle_grpc_error(exc)


def _legacy_job_control_error(exc: Exception) -> JSONResponse | None:
    details = getattr(exc, "details", None)
    if not callable(details):
        return None
    try:
        detail = str(details())
    except Exception:
        return None
    if not detail:
        return None
    return JSONResponse(status_code=500, content={"version": 1, "error": detail})
