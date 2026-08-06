from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from mn_sdk import (
    RuntimeService,
    generate_job_definition_submission_id,
    generate_stable_job_id,
)

from mn_api import state
from mn_api.blueprints import create_blueprint_run_id, find_blueprint, load_blueprint_bundle
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth, require_websocket_auth
from mn_api.errors import handle_grpc_error
from mn_api.routes import blueprints as blueprint_routes
from mn_api.routes import runs as runtime_run_routes
from mn_api.routes.jobs import (
    _compact_job_detail,
    _workflow_progress_snapshot_for_job,
)
from mn_api.schemas import (
    BlueprintRunRequest,
    BlueprintRunV2Request,
    ConfirmDeleteRequest,
    JobScheduleCreateRequest,
    StableJobCreateRequest,
    StableJobUpdateRequest,
    StartRunRequest,
)


router = APIRouter(prefix="/api/v2")
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "canceled"}


def _service() -> RuntimeService:
    return RuntimeService(state.client)


def _v2_run_record(run_id: str) -> dict:
    record = _service().get_run(run_id)
    return record if isinstance(record, dict) else json.loads(record)


def _v2_payload(value):
    if isinstance(value, dict):
        return {key: _v2_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_v2_payload(item) for item in value]
    if isinstance(value, str):
        rewritten = value.replace("/api/v1/runs/", "/api/v2/runtime-runs/")
        return re.sub(
            r"/api/v1/jobs/([^?]+)(\?.*)?$",
            r"/api/v2/runs/\1/monitor\2",
            rewritten,
        )
    return value


def _v2_progress_snapshot(run_id: str) -> dict:
    run = _v2_run_record(run_id)
    snapshot = _workflow_progress_snapshot_for_job(run_id)
    runtime_run_id = snapshot.get("run_id")
    return _v2_payload({
        **snapshot,
        "version": 2,
        "schema_version": 2,
        "job_id": run.get("job_id"),
        "run_id": run_id,
        "execution_id": run_id,
        "runtime_run_id": runtime_run_id if runtime_run_id != run_id else None,
        "graph_id": run.get("graph_id") or snapshot.get("workflow_id"),
        "status": run.get("status") or snapshot.get("status"),
    })


def _v2_monitor_snapshot(run_id: str) -> dict:
    run = _v2_run_record(run_id)
    detail = _compact_job_detail(run_id)
    runtime = detail.get("job") if isinstance(detail.get("job"), dict) else {}
    return _v2_payload({
        **{key: value for key, value in detail.items() if key != "job"},
        "version": 2,
        "job": run,
        "runtime": runtime,
        "job_id": run.get("job_id"),
        "run_id": run_id,
        "execution_id": run_id,
        "runtime_run_id": runtime.get("run_id"),
        "status": run.get("status") or runtime.get("status"),
        "graph_id": run.get("graph_id") or runtime.get("graph_id"),
    })


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post("/blueprints/{blueprint_id}/runs")
def run_blueprint(
    blueprint_id: str,
    request: BlueprintRunV2Request,
    _auth=Depends(require_auth),
):
    launch_request = BlueprintRunRequest(
        version=2,
        job_id=request.job_id,
        run_id=request.run_id,
        config_overrides=request.config_overrides,
        force=request.force,
        progress_id=request.progress_id,
        fake_llm=request.fake_llm,
        fake_skills=request.fake_skills,
    )
    result = blueprint_routes.run_blueprint(blueprint_id, launch_request, _auth)
    if isinstance(result, JSONResponse):
        return result
    execution_id = str(result.get("run_id") or result.get("id") or "")
    response = {
        **result,
        "version": 2,
        "execution_id": execution_id or None,
        "run_id": execution_id or None,
    }
    if result.get("progress_id"):
        response["progress_url"] = (
            f"/api/v2/blueprints/launch/progress/{result['progress_id']}"
        )
    else:
        response.pop("progress_url", None)
    return _v2_payload(response)


@router.get("/blueprints/launch/progress/{progress_id}")
def get_blueprint_launch_progress(progress_id: str, _auth=Depends(require_auth)):
    result = blueprint_routes.launch_progress_snapshot(progress_id)
    return _v2_payload(
        {
            **result,
            "version": 2,
            "schema_version": "mn.launch_progress.v2",
        }
    )


@router.post("/jobs")
def create_job(request: StableJobCreateRequest, _auth=Depends(require_auth)):
    try:
        if request.blueprint_id:
            repo_root, blueprint = find_blueprint(
                state.refresh_config_from_env(), request.blueprint_id
            )
            bootstrap_run_id = create_blueprint_run_id(request.blueprint_id)
            stable_job_id = request.job_id or generate_stable_job_id(
                request.blueprint_id
            )
            manifest_json, payloads = load_blueprint_bundle(
                repo_root,
                blueprint,
                bootstrap_run_id,
                config_overrides=request.resolved_configuration,
                stable_job_id=stable_job_id,
                submission_id=generate_job_definition_submission_id(stable_job_id),
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
            job_id=(
                stable_job_id
                if request.blueprint_id
                else request.job_id
            ),
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
        if request.manifest_json is None:
            return _service().update_stable_job(job_id, request.attrs)
        return _service().update_stable_job(
            job_id,
            request.attrs,
            manifest_json=request.manifest_json,
            payloads={
                key: value.encode("utf-8")
                for key, value in (request.payloads or {}).items()
            },
        )
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


@router.get("/runs/{run_id}/monitor")
def get_run_monitor(run_id: str, _auth=Depends(require_auth)):
    try:
        return _v2_monitor_snapshot(run_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/runs/{run_id}/workflow-progress")
def get_run_workflow_progress(run_id: str, _auth=Depends(require_auth)):
    try:
        return _v2_progress_snapshot(run_id)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/runs/{run_id}/workflow-progress/stream")
def stream_run_workflow_progress(
    run_id: str,
    interval: float = Query(1.0, ge=0.25, le=30.0),
    _auth=Depends(require_auth),
):
    def event_source():
        previous = ""
        while True:
            snapshot = _v2_progress_snapshot(run_id)
            serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            if serialized != previous:
                yield _sse_event("snapshot", snapshot)
                previous = serialized
            if str(snapshot.get("status") or "").lower() in _TERMINAL_RUN_STATUSES:
                break
            time.sleep(max(float(interval), 0.25))

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.websocket("/runs/{run_id}/workflow-progress/ws")
async def websocket_run_workflow_progress(
    websocket: WebSocket,
    run_id: str,
    interval: float = Query(1.0, ge=0.25, le=30.0),
):
    await require_websocket_auth(websocket)
    await websocket.accept()
    previous = ""
    try:
        while True:
            snapshot = _v2_progress_snapshot(run_id)
            serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            if serialized != previous:
                await websocket.send_json({"event": "snapshot", "data": snapshot})
                previous = serialized
            if str(snapshot.get("status") or "").lower() in _TERMINAL_RUN_STATUSES:
                await websocket.close(code=1000)
                break
            await asyncio.sleep(max(float(interval), 0.25))
    except WebSocketDisconnect:
        pass


@router.get("/runtime-runs/{run_id}/logs")
def get_runtime_run_logs(
    run_id: str,
    level: str | None = Query(default=None),
    limit: int = Query(200, ge=0, le=5000),
    since: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.get_run_logs(run_id, level, limit, since, _auth))


@router.get("/runtime-runs/{run_id}/resources")
def get_runtime_run_resources(
    run_id: str,
    window: str = Query("24h"),
    bucket: str = Query("1h"),
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.get_run_resources(run_id, window, bucket, _auth))


@router.get("/runtime-runs/{run_id}/events")
def get_runtime_run_events(
    run_id: str,
    limit: int = Query(200, ge=0, le=5000),
    channel: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.get_run_events(run_id, limit, channel, _auth))


@router.get("/runtime-runs/{run_id}/human")
def get_runtime_run_human_events(
    run_id: str,
    status: str | None = Query(default=None),
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.get_run_human_events(run_id, status, _auth))


@router.post("/runtime-runs/{run_id}/human/{request_id}/response")
def post_runtime_run_human_response(
    run_id: str,
    request_id: str,
    payload: dict,
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.post_run_human_response(run_id, request_id, payload, _auth))


@router.post("/runtime-runs/{run_id}/human/{notice_id}/ack")
def post_runtime_run_human_ack(
    run_id: str,
    notice_id: str,
    payload: dict | None = None,
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.post_run_human_ack(run_id, notice_id, payload, _auth))


@router.get("/runtime-runs/{run_id}/ui")
def get_runtime_run_ui(
    run_id: str,
    limit: int = Query(200, ge=0, le=1000),
    _auth=Depends(require_auth),
):
    return _v2_payload(runtime_run_routes.get_run_ui(run_id, limit, _auth))


@router.get("/runtime-runs/{run_id}/ui/video")
def get_runtime_run_ui_video(run_id: str, _auth=Depends(require_auth)):
    return runtime_run_routes.get_run_ui_video(run_id, _auth)


@router.get("/runtime-runs/{run_id}/final-artifact")
def get_runtime_run_final_artifact(run_id: str, _auth=Depends(require_auth)):
    return _v2_payload(runtime_run_routes.get_run_final_artifact(run_id, _auth))


@router.get("/runtime-runs/{run_id}/artifacts")
def get_runtime_run_artifacts(run_id: str, _auth=Depends(require_auth)):
    return _v2_payload(runtime_run_routes.list_run_artifacts(run_id, _auth))


@router.post("/runtime-runs/{run_id}/artifacts/{artifact_path:path}/reveal")
def reveal_runtime_run_artifact(
    run_id: str,
    artifact_path: str,
    _auth=Depends(require_auth),
):
    return _v2_payload(
        runtime_run_routes.reveal_run_artifact(run_id, artifact_path, _auth)
    )


@router.get("/runtime-runs/{run_id}/artifacts/{artifact_path:path}")
def get_runtime_run_artifact(
    run_id: str,
    artifact_path: str,
    _auth=Depends(require_auth),
):
    return runtime_run_routes.get_run_artifact(run_id, artifact_path, _auth)


@router.get("/runtime-runs/{run_id}/outputs")
def get_runtime_run_outputs(run_id: str, _auth=Depends(require_auth)):
    return _v2_payload(runtime_run_routes.list_run_outputs(run_id, _auth))


@router.post("/runtime-runs/{run_id}/outputs/{output_index}/reveal")
def reveal_runtime_run_output(
    run_id: str,
    output_index: int,
    _auth=Depends(require_auth),
):
    return _v2_payload(
        runtime_run_routes.reveal_run_output(run_id, output_index, _auth)
    )


@router.get("/runtime-runs/{run_id}/outputs/{output_index}")
def get_runtime_run_output(
    run_id: str,
    output_index: int,
    _auth=Depends(require_auth),
):
    return runtime_run_routes.get_run_output(run_id, output_index, _auth)


@router.get("/runtime-runs/{run_id}/observability-summary")
def get_runtime_run_observability_summary(run_id: str, _auth=Depends(require_auth)):
    return _v2_payload(runtime_run_routes.get_run_observability_summary(run_id, _auth))


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
