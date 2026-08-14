from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response, status
from mn_sdk import RuntimeService

from mn_api import state
from mn_api.api_models import DispatchCreate, PageResponse, ResourceModel, ScheduleUpdate, TriggerEventCreate
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.http_semantics import require_if_match
from mn_api.pagination import page
from mn_api.public import idempotent_response, public_value, records, resource_response


router = APIRouter(prefix=API_PREFIX)


def _service() -> RuntimeService:
    return RuntimeService(state.client)


@router.get("/schedules", operation_id="list_schedules", tags=["schedules"], response_model=PageResponse)
def list_schedules(
    kind: str | None = None,
    schedule_status: str | None = Query(default=None, alias="status"),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    items = records(_service().list_schedules(kind=kind, status=schedule_status), "items", "schedules", "data")
    return page(
        items,
        route=f"{API_PREFIX}/schedules",
        principal=principal,
        filters={"kind": kind, "status": schedule_status},
        page_size=page_size,
        page_token=page_token,
        sort_key="created_at,schedule_id",
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("schedule_id") or item.get("id") or "")),
        identity=lambda item: str(item.get("schedule_id") or item.get("id") or ""),
    )


@router.get("/schedules/{schedule_id}", operation_id="get_schedule", tags=["schedules"], response_model=ResourceModel)
def get_schedule(schedule_id: str, _principal=Depends(require_auth)):
    return resource_response(_service().get_schedule(schedule_id), etag=True)


@router.patch("/schedules/{schedule_id}", operation_id="update_schedule", tags=["schedules"], response_model=ResourceModel)
def update_schedule(
    schedule_id: str,
    request: ScheduleUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_schedule(schedule_id))
    require_if_match(if_match, current)
    if request.desired_state == "paused":
        result = _service().pause_schedule(schedule_id, reason=request.reason)
    elif request.desired_state == "running":
        result = _service().resume_schedule(schedule_id, reason=request.reason)
    else:
        attrs = request.model_dump(exclude_none=True, exclude={"reason", "desired_state"})
        result = _service().update_schedule(schedule_id, attrs=attrs, reason=request.reason)
    return resource_response(result, etag=True)


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_schedule", tags=["schedules"])
def delete_schedule(
    schedule_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_schedule(schedule_id))
    require_if_match(if_match, current)
    _service().delete_schedule(schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/schedules/{schedule_id}/dispatches",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_schedule_dispatch",
    tags=["schedules"],
    response_model=ResourceModel,
)
def create_schedule_dispatch(
    schedule_id: str,
    request: DispatchCreate | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    payload = request or DispatchCreate()
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/schedules/{schedule_id}/dispatches",
        key=idempotency_key,
        body=payload.model_dump(),
        call=lambda: _service().dispatch_schedule(schedule_id, payload=payload.payload, reason=payload.reason),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/runs/{result.get('run_id') or result.get('id')}",
    )


@router.post(
    "/trigger-events",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_trigger_event",
    tags=["trigger-events"],
    response_model=ResourceModel,
)
def create_trigger_event(request: TriggerEventCreate, _principal=Depends(require_auth)):
    return public_value(_service().emit_trigger_event(request.event_type, payload=request.payload, source=request.source))


@router.get("/trigger-events", operation_id="list_trigger_events", tags=["trigger-events"], response_model=PageResponse)
def list_trigger_events(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    items = records(_service().list_trigger_events(limit=5000), "items", "events", "data")
    return page(
        items,
        route=f"{API_PREFIX}/trigger-events",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
        sort_key="created_at,event_id",
        key=lambda item: (str(item.get("created_at") or item.get("occurred_at") or ""), str(item.get("event_id") or item.get("id") or "")),
        identity=lambda item: str(item.get("event_id") or item.get("id") or ""),
    )
