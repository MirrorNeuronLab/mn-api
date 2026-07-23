from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mn_sdk import RuntimeService

from mn_api import state
from mn_api.blueprints import create_blueprint_run_id, find_blueprint, load_blueprint_bundle
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import (
    ConfirmDeleteRequest,
    JobScheduleCreateRequest,
    StableJobCreateRequest,
    StableJobUpdateRequest,
    StartRunRequest,
)


router = APIRouter(prefix="/api/v2")


def _service() -> RuntimeService:
    return RuntimeService(state.client)


@router.post("/jobs")
def create_job(request: StableJobCreateRequest, _auth=Depends(require_auth)):
    try:
        if request.blueprint_id:
            repo_root, blueprint = find_blueprint(
                state.refresh_config_from_env(), request.blueprint_id
            )
            bootstrap_run_id = create_blueprint_run_id(request.blueprint_id)
            manifest_json, payloads = load_blueprint_bundle(
                repo_root,
                blueprint,
                bootstrap_run_id,
                config_overrides=request.resolved_configuration,
            )
            bundle_dir = None
        elif request.bundle_path:
            manifest_json, payloads = load_uploaded_bundle(
                request.bundle_path, state.BUNDLE_UPLOAD_ROOT
            )
            bundle_dir = request.bundle_path
        elif request.manifest_json is not None:
            manifest_json = request.manifest_json
            payloads = {
                key: value.encode("utf-8")
                for key, value in (request.payloads or {}).items()
            }
            bundle_dir = None
        else:
            raise HTTPException(
                status_code=422,
                detail="blueprint_id, manifest_json, or _bundle_path is required",
            )
        return _service().create_stable_job(
            manifest_json,
            payloads,
            bundle_dir=bundle_dir,
            job_id=request.job_id,
            resolved_configuration=request.resolved_configuration,
            storage=request.storage,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs")
def list_jobs(include_archived: bool = False, _auth=Depends(require_auth)):
    try:
        return _service().list_stable_jobs(include_archived=include_archived)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, _auth=Depends(require_auth)):
    try:
        return _service().get_stable_job(job_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.patch("/jobs/{job_id}")
def update_job(
    job_id: str, request: StableJobUpdateRequest, _auth=Depends(require_auth)
):
    try:
        return _service().update_stable_job(job_id, request.attrs)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/archive")
def archive_job(job_id: str, _auth=Depends(require_auth)):
    try:
        return _service().archive_stable_job(job_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/data:reset")
def reset_job_data(job_id: str, _auth=Depends(require_auth)):
    try:
        return _service().reset_stable_job_data(job_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: str, request: ConfirmDeleteRequest, _auth=Depends(require_auth)
):
    try:
        return _service().delete_stable_job(job_id, confirmed=request.confirmed)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/runs")
def start_run(job_id: str, request: StartRunRequest, _auth=Depends(require_auth)):
    try:
        return _service().start_run(
            job_id, run_id=request.run_id, inputs=request.inputs
        )
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/jobs/{job_id}/runs")
def list_runs(job_id: str, _auth=Depends(require_auth)):
    try:
        return _service().list_runs(job_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/jobs/{job_id}/schedules")
def create_job_schedule(
    job_id: str,
    request: JobScheduleCreateRequest,
    _auth=Depends(require_auth),
):
    try:
        return _service().create_job_schedule(
            job_id, schedule=request.schedule, source=request.source
        )
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/runs/{run_id}")
def get_run(run_id: str, _auth=Depends(require_auth)):
    try:
        return _service().get_run(run_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/runs/{run_id}/pause")
def pause_run(run_id: str, _auth=Depends(require_auth)):
    try:
        return _service().pause_run(run_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, _auth=Depends(require_auth)):
    try:
        return _service().resume_run(run_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, _auth=Depends(require_auth)):
    try:
        return _service().cancel_run(run_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: str, request: ConfirmDeleteRequest, _auth=Depends(require_auth)
):
    try:
        return _service().delete_run(run_id, confirmed=request.confirmed)
    except Exception as exc:
        return handle_grpc_error(exc)
