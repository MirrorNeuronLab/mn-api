from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from mn_api import state
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.routes.client_json import client_json_response
from mn_api.schemas import (
    CreateScheduleRequest,
    DispatchScheduleRequest,
    EmitEventRequest,
    ScheduleUpdateRequest,
)


router = APIRouter(prefix="/api/v2")


@router.post("/schedules")
def create_schedule(req: CreateScheduleRequest, _auth=Depends(require_auth)):
    try:
        manifest_json, payloads = _manifest_and_payloads(req)
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)

    try:
        return JSONResponse(
            content=json.loads(
                state.client.create_schedule(
                    manifest_json,
                    payloads,
                    schedule=req.schedule,
                    source=req.source or {"api": "create_schedule"},
                )
            )
        )
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/triggers")
def create_trigger(req: CreateScheduleRequest, _auth=Depends(require_auth)):
    req.schedule = {**(req.schedule or {}), "kind": "event"}
    return create_schedule(req, _auth=_auth)


@router.get("/triggers")
def list_triggers(_auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.list_schedules(kind="event"), add_version=False)


@router.delete("/triggers/{schedule_id}")
def delete_trigger(schedule_id: str, reason: str = "", _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.delete_schedule(schedule_id, reason=reason), add_version=False)


@router.post("/schedules/periodic")
def create_periodic_schedule(req: CreateScheduleRequest, _auth=Depends(require_auth)):
    req.schedule = {**(req.schedule or {}), "kind": "periodic"}
    return create_schedule(req, _auth=_auth)


@router.post("/schedules/delayed")
def create_delayed_schedule(req: CreateScheduleRequest, _auth=Depends(require_auth)):
    req.schedule = {**(req.schedule or {}), "kind": "delayed"}
    return create_schedule(req, _auth=_auth)


@router.get("/schedules")
def list_schedules(kind: str | None = None, status: str | None = None, _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.list_schedules(kind=kind, status=status), add_version=False)


@router.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: str, _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.get_schedule(schedule_id), add_version=False)


@router.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, req: ScheduleUpdateRequest, _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.update_schedule(schedule_id, req.attrs, reason=req.reason), add_version=False)


@router.post("/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: str, req: ScheduleUpdateRequest | None = None, _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.pause_schedule(schedule_id, reason=(req.reason if req else "")), add_version=False)


@router.post("/schedules/{schedule_id}/resume")
def resume_schedule(schedule_id: str, req: ScheduleUpdateRequest | None = None, _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.resume_schedule(schedule_id, reason=(req.reason if req else "")), add_version=False)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, reason: str = "", _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.delete_schedule(schedule_id, reason=reason), add_version=False)


@router.post("/schedules/{schedule_id}/dispatch")
def dispatch_schedule(schedule_id: str, req: DispatchScheduleRequest, _auth=Depends(require_auth)):
    return client_json_response(
        lambda: state.client.dispatch_schedule(schedule_id, payload=req.payload, reason=req.reason),
        add_version=False,
    )


@router.post("/events")
def emit_event(req: EmitEventRequest, _auth=Depends(require_auth)):
    return client_json_response(
        lambda: state.client.emit_trigger_event(req.event_type, payload=req.payload, source=req.source),
        add_version=False,
    )


@router.get("/events")
def list_events(limit: int = 100, _auth=Depends(require_auth)):
    return client_json_response(lambda: state.client.list_trigger_events(limit=limit), add_version=False)


def _manifest_and_payloads(req: CreateScheduleRequest):
    if req.bundle_path:
        return load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
    if req.manifest_json is None:
        raise HTTPException(status_code=422, detail="manifest_json or _bundle_path is required")
    payloads = {key: value.encode("utf-8") for key, value in (req.payloads or {}).items()}
    return req.manifest_json, payloads
