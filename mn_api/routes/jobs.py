from __future__ import annotations

import gzip
import json
import os
import queue
import re
import threading
import urllib.parse
from collections import Counter, deque
from pathlib import Path
from typing import Any

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from mn_sdk import (
    BlueprintWorkflowProgress,
    make_validation_report,
    prepare_job_submission,
    run_hardware_requirements_validation,
    run_input_validation,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
    validate_resource_spec_issues,
    failure_from_event,
    normalize_error,
    workflow_progress_snapshot,
)

from mn_api import state
from mn_api.agent_graph import build_agent_graph
from mn_api.artifacts import artifact_ref, list_artifact_files
from mn_api.blueprints import cleanup_blueprint_processes_for_job
from mn_api.blueprints import runtime_resource_report
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
from mn_api.run_outputs import output_refs
from mn_api.schemas import SubmitJobRequest


router = APIRouter(prefix="/api/v1")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_MAX_COMPACT_STRING = 2000
_MAX_COMPACT_LIST = 25
_MAX_COMPACT_DEPTH = 5
_MAX_ACTIVITY_EVENTS = 8
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

        prepared = prepare_job_submission(
            manifest_json,
            payloads_bytes,
            bundle_dir=bundle_dir,
            run_id=_submission_run_id(manifest_json),
        )
        manifest_json = prepared.manifest_json
        payloads_bytes = prepared.payloads

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
    manifest = _decode_manifest(manifest_json)
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
    manifest = _decode_manifest(manifest_json)
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


def _decode_manifest(manifest_json: str) -> dict:
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="manifest_json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="manifest_json must be an object")
    return manifest


def _submission_run_id(manifest_json: str) -> str | None:
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict):
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


@router.post("/jobs:cleanup")
@router.post("/jobs/cleanup")
def cleanup_jobs(_auth=Depends(require_auth)):
    try:
        cleared_count = state.client.clear_jobs()
        return {"cleared_count": cleared_count}
    except Exception as exc:
        if _is_clear_jobs_admin_token_error(exc):
            state.close_client()
            try:
                cleared_count = state.client.clear_jobs()
                return {"cleared_count": cleared_count}
            except Exception as retry_exc:
                return handle_grpc_error(retry_exc)
        return handle_grpc_error(exc)


def _is_clear_jobs_admin_token_error(exc: Exception) -> bool:
    if not isinstance(exc, grpc.RpcError):
        return False
    if exc.code() != grpc.StatusCode.PERMISSION_DENIED:
        return False
    return "MN_GRPC_ADMIN_TOKEN" in str(exc.details())


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
    return bool(job_id and job_id != "unknown")


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
        "data": [
            _reconciled_job_list_row(job) if isinstance(job, dict) else job
            for job in jobs
        ],
    }


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl_file(path: Path, *, limit: int = 5000) -> list[dict[str, Any]]:
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
                    event = {"type": "unparseable_event", "payload": {"line": stripped[:_MAX_COMPACT_STRING]}}
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return list(events)


def _stream_jsonl_files(run_dir: Path, file_name: str) -> list[Path]:
    paths: list[Path] = []
    index_path = run_dir / f"{Path(file_name).stem}.index.json"
    if index_path.exists():
        index = _read_json_file(index_path)
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
        for event_json in state.client.stream_events(job_id, follow=False):
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
    failure = failure_from_event(event)
    compact = {
        "type": event.get("type"),
        "timestamp": event.get("timestamp") or event.get("ts"),
        "agent_id": event.get("agent_id") or event.get("node_id"),
        "status": event.get("status"),
    }
    if failure:
        compact["failure"] = {
            "schema_version": failure.get("schema_version"),
            "code": failure.get("code"),
            "desc": failure.get("desc"),
            "severity": failure.get("severity"),
            "details": _compact_value(failure.get("details")),
            "remediation": failure.get("remediation"),
            "links": failure.get("links"),
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
    if stream_error:
        summary["event_stream_warning"] = stream_error
    return {
        "job": job,
        "summary": summary,
        "trace_id": trace_id or None,
        "observability_summary": observability_summary,
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


def _workflow_progress_snapshot_for_job(job_id: str) -> dict[str, Any]:
    details = _full_job_detail(job_id)
    events, stream_error = _stream_job_events(job_id, limit=5000)
    run_dir = _run_dir_for_details(details, events, job_id=job_id)
    events = _merge_events(events, _run_store_events(run_dir, limit=5000), limit=5000)
    observability_summary = _read_json_file(run_dir / "observability_summary.json") if run_dir else {}
    snapshot = workflow_progress_snapshot(
        _manifest_from_job_details(details, run_dir=run_dir),
        events,
        job=_job_from_details(details),
        summary=_summary_from_details(details),
        job_id=job_id,
    )
    _apply_default_assigned_node(snapshot, details)
    _enrich_workflow_progress_activity(snapshot, events)
    _clear_success_failure(snapshot)
    if not snapshot.get("failure") and not _is_success_status(snapshot.get("status")):
        failure = _failure_from_sources(events, _job_from_details(details), _summary_from_details(details))
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
    direct_manifest = _first_manifest(
        details.get("manifest"),
        _job_from_details(details).get("manifest"),
        _summary_from_details(details).get("manifest"),
    )
    if _manifest_has_workflow_flow(direct_manifest):
        return direct_manifest

    run_manifest = _manifest_from_run_dir(run_dir)
    if _manifest_has_workflow_flow(run_manifest):
        return run_manifest

    manifest_ref = _job_from_details(details).get("manifest_ref")
    if not isinstance(manifest_ref, dict):
        manifest_ref = _summary_from_details(details).get("manifest_ref")
    ref_manifest: dict[str, Any] = {}
    if isinstance(manifest_ref, dict):
        for raw_path in (
            manifest_ref.get("manifest_path"),
            Path(str(manifest_ref.get("job_path") or "")) / "manifest.json"
            if manifest_ref.get("job_path")
            else None,
        ):
            if not raw_path:
                continue
            try:
                path = Path(str(raw_path)).expanduser()
                if path.is_file():
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        ref_manifest = loaded
                        break
            except (OSError, json.JSONDecodeError):
                continue
    if _manifest_has_workflow_flow(ref_manifest):
        return ref_manifest
    if run_manifest:
        return run_manifest
    if direct_manifest:
        return direct_manifest
    if ref_manifest:
        return ref_manifest

    return _fallback_manifest_from_details(details)


def _first_manifest(*candidates: Any) -> dict[str, Any]:
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _manifest_has_workflow_flow(manifest: dict[str, Any]) -> bool:
    workflow = manifest.get("workflow") if isinstance(manifest, dict) else None
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    return isinstance(steps, list) and bool(steps)


def _manifest_from_run_dir(run_dir: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return {}
    for filename in ("config.json", "manifest.json"):
        manifest = _read_json_file(run_dir / filename)
        if manifest:
            return manifest
    return {}


def _run_dir_for_details(details: dict[str, Any], events: list[dict[str, Any]], *, job_id: str | None = None) -> Path | None:
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


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_step_id(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return _first_string(
        payload.get("step"),
        payload.get("step_id"),
        payload.get("phase"),
        payload.get("phase_id"),
        event.get("step"),
        event.get("step_id"),
        event.get("phase"),
        event.get("phase_id"),
    )


def _event_agent_id(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    return _first_string(
        payload.get("worker"),
        payload.get("agent_id"),
        payload.get("node_id"),
        event.get("worker"),
        event.get("agent_id"),
        event.get("node_id"),
    )


def _humanize_event_type(event_type: Any) -> str:
    text = re.sub(r"[_-]+", " ", str(event_type or "")).strip()
    return " ".join(text.split()).capitalize()


def _compact_activity_text(value: Any, limit: int = 320, *, prefer_tail: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    if prefer_tail:
        suffix = text[-max(limit - 15, 0) :].lstrip()
        return "[truncated] " + suffix
    return text[: max(limit - 15, 0)].rstrip() + " [truncated]"


def _event_category(event: dict[str, Any], payload: dict[str, Any], failure: dict[str, Any] | None = None) -> str:
    category = _first_string(payload.get("category"), event.get("category"))
    if category in {"agent", "tool", "system", "artifact", "error"}:
        return category
    event_type = str(event.get("type") or "").lower()
    if failure or "failed" in event_type or "error" in event_type or "timed_out" in event_type or "retry" in event_type:
        return "error"
    if event_type.startswith("tool_") or "tool_call" in event_type:
        return "tool"
    if event_type in {"artifact_written"} or "artifact" in event_type:
        return "artifact"
    if event_type.startswith("docker_worker_") or event_type.startswith("executor_") or event_type.startswith("workflow_") or event_type.startswith("sandbox_"):
        return "system"
    if event_type.startswith("financial_") or event_type in {"agent_activity", "blueprint_phase_started", "blueprint_phase_completed"}:
        return "agent"
    return "system"


def _activity_message(event: dict[str, Any], *, step_id: str = "", agent_id: str = "") -> str:
    event_type = str(event.get("type") or "")
    payload = _event_payload(event)
    failure = failure_from_event(event)
    message = _first_string(
        payload.get("message"),
        event.get("message"),
        payload.get("result_summary"),
        payload.get("working_on"),
        payload.get("task"),
        payload.get("reason"),
        event.get("reason"),
        payload.get("status_reason"),
        payload.get("status"),
        event.get("status"),
    )
    if message:
        return _compact_activity_text(message)
    if failure:
        return _compact_activity_text(_first_string(failure.get("desc"), failure.get("code"), "Failure"))
    normalized = re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")
    if normalized == "docker_worker_build_started":
        return "DockerWorker image build started"
    if normalized == "docker_worker_build_completed":
        return "DockerWorker image build completed"
    if normalized == "docker_worker_build_failed":
        return "DockerWorker image build failed"
    if normalized == "docker_worker_command_started":
        return "DockerWorker command started"
    if normalized == "docker_worker_command_completed":
        return "DockerWorker command completed"
    if normalized == "docker_worker_command_timed_out":
        return "DockerWorker command timed out"
    if normalized in {"workflow_step_attempt_completed", "sandbox_job_completed"}:
        return f"Agent completed: {agent_id or _event_agent_id(event) or 'unknown'}"
    if normalized in {"workflow_worker_started", "workflow_step_attempt_started"}:
        return f"Agent working: {agent_id or _event_agent_id(event) or 'unknown'}"
    if normalized in {"workflow_step_completed", "blueprint_phase_completed"}:
        return f"Step completed: {step_id or _event_step_id(event) or 'step'}"
    if normalized in {"workflow_step_started", "blueprint_phase_started"}:
        return f"Step started: {step_id or _event_step_id(event) or 'step'}"
    if normalized in {"workflow_step_attempt_retry_scheduled", "workflow_step_attempt_timed_out"}:
        return f"Retry pending: {step_id or _event_step_id(event) or 'step'}"
    if normalized == "workflow_step_blocked":
        return f"Blocked: {step_id or _event_step_id(event) or 'step'}"
    return _humanize_event_type(event_type or "event")


def _compact_activity_event(event: dict[str, Any], *, step_id: str = "", agent_id: str = "") -> dict[str, Any]:
    payload = _event_payload(event)
    failure = failure_from_event(event)
    category = _event_category(event, payload, failure)
    compact = {
        "timestamp": event.get("timestamp") or event.get("ts"),
        "type": event.get("type"),
        "category": category,
        "step_id": step_id or _event_step_id(event),
        "agent_id": agent_id or _event_agent_id(event),
        "status": _first_string(event.get("status"), payload.get("status")),
        "message": _activity_message(event, step_id=step_id, agent_id=agent_id),
    }
    for key in ("tool_name", "target", "duration_ms", "result_summary", "details"):
        value = payload.get(key)
        if value not in (None, "", {}):
            if key == "details":
                compact[key] = _compact_value(value)
            elif isinstance(value, str):
                compact[key] = _compact_activity_text(value, prefer_tail=key == "result_summary")
            else:
                compact[key] = value
    if payload:
        compact["payload"] = _compact_value(payload)
    if failure:
        compact["failure"] = {
            "code": failure.get("code"),
            "desc": _compact_activity_text(failure.get("desc")),
            "severity": failure.get("severity"),
        }
    return {key: value for key, value in compact.items() if value not in (None, "")}


def _agent_ids_match(known: str, observed: str) -> bool:
    if known == observed:
        return True
    known_tail = known.split(":")[-1]
    observed_tail = observed.split(":")[-1]
    return known_tail == observed_tail or known.endswith(f":{observed}") or observed.endswith(f":{known}")


def _agent_step_id(agent_to_step: dict[str, str], agent_id: str) -> str:
    if not agent_id:
        return ""
    if agent_id in agent_to_step:
        return agent_to_step[agent_id]
    for known_agent_id, step_id in agent_to_step.items():
        if _agent_ids_match(known_agent_id, agent_id):
            return step_id
    return ""


def _enrich_workflow_progress_activity(snapshot: dict[str, Any], events: list[dict[str, Any]]) -> None:
    steps = snapshot.get("steps")
    if not isinstance(steps, list) or not steps:
        return

    steps_by_id: dict[str, dict[str, Any]] = {}
    agent_to_step: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = _first_string(step.get("id"))
        if not step_id:
            continue
        steps_by_id[step_id] = step
        agents = step.get("agents")
        if isinstance(agents, list):
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                agent_id = _first_string(agent.get("id"), agent.get("agent_id"))
                if agent_id:
                    agent_to_step[agent_id] = step_id

    step_events: dict[str, list[dict[str, Any]]] = {step_id: [] for step_id in steps_by_id}
    agent_events: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        step_id = _event_step_id(event)
        agent_id = _event_agent_id(event)
        if step_id not in steps_by_id and agent_id:
            step_id = _agent_step_id(agent_to_step, agent_id)
        if not step_id or step_id not in steps_by_id:
            continue
        compact = _compact_activity_event(event, step_id=step_id, agent_id=agent_id)
        step_events.setdefault(step_id, []).append(compact)
        if agent_id:
            agent_events.setdefault((step_id, agent_id), []).append(compact)

    for step_id, step in steps_by_id.items():
        recent = step_events.get(step_id, [])[-_MAX_ACTIVITY_EVENTS:]
        if recent:
            step["recent_events"] = recent
            step["last_activity"] = recent[-1]
            step["activity_summary"] = _first_string(recent[-1].get("message"), recent[-1].get("type"))
        agents = step.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = _first_string(agent.get("id"), agent.get("agent_id"))
            if not agent_id:
                continue
            recent_agent_events: list[dict[str, Any]] = []
            for (event_step_id, event_agent_id), values in agent_events.items():
                if event_step_id == step_id and _agent_ids_match(agent_id, event_agent_id):
                    recent_agent_events.extend(values)
            recent_agent_events = recent_agent_events[-_MAX_ACTIVITY_EVENTS:]
            if recent_agent_events:
                agent["recent_events"] = recent_agent_events
                agent["last_activity"] = recent_agent_events[-1]
                agent["activity_summary"] = _first_string(
                    recent_agent_events[-1].get("message"),
                    recent_agent_events[-1].get("type"),
                )

    current_step = snapshot.get("current_step")
    if isinstance(current_step, dict):
        step_id = _first_string(current_step.get("id"))
        enriched = steps_by_id.get(step_id)
        if enriched:
            snapshot["current_step"] = {**enriched, "current": current_step.get("current", enriched.get("current", True))}


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


@router.get("/jobs/{job_id}/agent-graph")
def get_job_agent_graph(job_id: str, _auth=Depends(require_auth)):
    try:
        details = _full_job_detail(job_id)
        events, _stream_error = _stream_job_events(job_id, limit=5000)
        run_dir = _run_dir_for_details(details, events, job_id=job_id)
        events = _merge_events(events, _run_store_events(run_dir, limit=5000), limit=5000)
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
            events, stream_error = _merged_job_events(job_id, limit=limit)
            response: dict[str, Any] = {"data": [_compact_event(event) for event in events[-limit:]]}
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
        events, stream_error = _stream_job_events(job_id, limit=5000)
        run_dir = _run_dir_for_details(details, events, job_id=job_id)
        manifest = _manifest_from_job_details(details, run_dir=run_dir)
        events = _merge_events(events, _run_store_events(run_dir, limit=5000), limit=5000)
        activity_events = list(events)
        observability_summary = _read_json_file(run_dir / "observability_summary.json") if run_dir else {}
        tracker = BlueprintWorkflowProgress(
            manifest,
            job_id=job_id,
            job=_job_from_details(details),
            summary=_summary_from_details(details),
        )
        seen = set()
        for event in events:
            seen.add(_event_key(event))
            tracker.update(event)
        tracker.apply_job_status(_job_from_details(details), _summary_from_details(details))
        initial = tracker.snapshot(job=_job_from_details(details), summary=_summary_from_details(details))
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
                    kind, payload = event_queue.get(timeout=interval)
                except queue.Empty:
                    yield ": heartbeat\n\n"
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
                    yield ": heartbeat\n\n"
                    continue
                event_key = _event_key(event)
                if event_key in seen:
                    continue
                seen.add(event_key)
                if len(seen) > 10_000:
                    seen.clear()

                activity_events = _merge_events(activity_events, [event], limit=5000)
                tracker.update(event)
                if event_type in {"job_completed", "job_failed", "job_cancelled"}:
                    details = _full_job_detail(job_id)
                    tracker.apply_job_status(_job_from_details(details), _summary_from_details(details))
                snapshot = tracker.snapshot(job=_job_from_details(details), summary=_summary_from_details(details))
                _apply_default_assigned_node(snapshot, details)
                _enrich_workflow_progress_activity(snapshot, activity_events)
                _clear_success_failure(snapshot)
                yield _sse_event(
                    "snapshot",
                    snapshot,
                )
                if event_type in {"job_completed", "job_failed", "job_cancelled"}:
                    break
        finally:
            stop.set()

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get("/jobs/{job_id}/dead-letters")
def get_job_dead_letters(job_id: str, _auth=Depends(require_auth)):
    try:
        dead_letters = []
        for event_index, event_json in enumerate(state.client.stream_events(job_id, follow=False)):
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
