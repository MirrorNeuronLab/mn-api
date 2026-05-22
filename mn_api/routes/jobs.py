from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from mn_sdk import run_input_validation, validate_input_validation_spec, validate_requirements_spec

from mn_api import state
from mn_api.agent_graph import build_agent_graph
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import SubmitJobRequest


router = APIRouter(prefix="/api/v1")


@router.post("/jobs")
def submit_job(req: SubmitJobRequest, _auth=Depends(require_auth)):
    try:
        if req.bundle_path:
            manifest_json, payloads_bytes = load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
            _validate_job_bundle(req.bundle_path, manifest_json, force=req.force)
        elif req.manifest_json is not None:
            manifest_json = req.manifest_json
            payloads_bytes = (
                {key: value.encode("utf-8") for key, value in req.payloads.items()}
                if req.payloads
                else {}
            )
            _validate_job_manifest(manifest_json, force=req.force)
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


def _validate_job_bundle(bundle_path: str, manifest_json: str, *, force: bool) -> None:
    if force:
        return
    manifest = _decode_manifest(manifest_json)
    spec_errors = validate_requirements_spec(manifest) + validate_input_validation_spec(manifest)
    if spec_errors:
        raise HTTPException(
            status_code=400,
            detail={"error": "manifest_validation_failed", "validation": {"ok": False, "errors": spec_errors}},
        )
    result = run_input_validation(Path(bundle_path), manifest)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail={"error": "input_validation_failed", "validation": result})


def _validate_job_manifest(manifest_json: str, *, force: bool) -> None:
    if force:
        return
    manifest = _decode_manifest(manifest_json)
    spec_errors = validate_requirements_spec(manifest) + validate_input_validation_spec(manifest)
    if spec_errors:
        raise HTTPException(
            status_code=400,
            detail={"error": "manifest_validation_failed", "validation": {"ok": False, "errors": spec_errors}},
        )
    validation = run_input_validation(Path.cwd(), manifest)
    if not validation.get("ok"):
        raise HTTPException(status_code=400, detail={"error": "input_validation_failed", "validation": validation})


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
def get_job(job_id: str, _auth=Depends(require_auth)):
    try:
        job_json = state.client.get_job(job_id)
        return json.loads(job_json)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/agent-graph")
def get_job_agent_graph(job_id: str, _auth=Depends(require_auth)):
    try:
        details = json.loads(state.client.get_job(job_id))
        events = [json.loads(event_json) for event_json in state.client.stream_events(job_id)]
        return build_agent_graph(job_id, details, events)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, _auth=Depends(require_auth)):
    try:
        events = []
        for event_json in state.client.stream_events(job_id):
            events.append(json.loads(event_json))
        return {"data": events}
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
        return {"status": status, "job_id": job_id}
    except Exception as exc:
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
