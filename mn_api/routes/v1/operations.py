from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from mn_api import state
from mn_api.api_models import CleanupCreate, PageResponse, ResourceModel
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.operations import (
    encode_sse,
    get_operation,
    is_local_operation,
    known_operations,
    sse_envelope,
    start_operation,
    stream_local_operation_events,
)
from mn_api.pagination import page
from mn_api.public import decode, idempotent_response, public_value


router = APIRouter(prefix=API_PREFIX)


def _administrative_run_operation(
    *,
    kind: str,
    request: CleanupCreate,
    idempotency_key: str | None,
    principal: str,
):
    route = f"{API_PREFIX}/{'run-cleanups' if kind == 'clear_jobs' else 'run-cancellations'}"
    body = request.model_dump()
    return idempotent_response(
        principal=principal,
        route=route,
        key=idempotency_key,
        body=body,
        call=lambda: start_operation(kind, body),
        status_code=202,
        location=lambda item: f"{API_PREFIX}/operations/{item.get('operation_id')}",
    )


@router.post(
    "/run-cleanups", operation_id="create_run_cleanup", tags=["operations"], status_code=202, response_model=ResourceModel
)
def create_run_cleanup(
    request: CleanupCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: str = Depends(require_auth),
):
    return _administrative_run_operation(
        kind="clear_jobs",
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )


@router.post(
    "/run-cancellations",
    operation_id="create_run_cancellation",
    tags=["operations"],
    status_code=202,
    response_model=ResourceModel,
)
def create_run_cancellation(
    request: CleanupCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: str = Depends(require_auth),
):
    return _administrative_run_operation(
        kind="cancel_all_jobs",
        request=request,
        idempotency_key=idempotency_key,
        principal=principal,
    )


@router.get("/operations", operation_id="list_operations", tags=["operations"], response_model=PageResponse)
def list_operations(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    items = known_operations()
    return page(
        items,
        route=f"{API_PREFIX}/operations",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
        sort_key="-created_at,-operation_id",
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("operation_id") or "")),
        identity=lambda item: str(item.get("operation_id") or ""),
        reverse=True,
    )


@router.get("/operations/{operation_id}", operation_id="get_operation", tags=["operations"], response_model=ResourceModel)
def operation(operation_id: str, _principal=Depends(require_auth)):
    return get_operation(operation_id)


@router.get(
    "/operations/{operation_id}/events/stream",
    operation_id="stream_operation_events",
    tags=["operations"],
)
def stream_operation_events(
    operation_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: str = Depends(require_auth),
):
    try:
        resume_after = max(int(last_event_id or "0"), 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer.") from exc

    def source():
        if is_local_operation(operation_id):
            yield ": heartbeat\n\n"
            for event in stream_local_operation_events(operation_id, resume_after=resume_after):
                yield encode_sse(
                    sse_envelope(
                        event_id=int(event["sequence"]),
                        event_type=str(event["type"]),
                        resource=f"{API_PREFIX}/operations/{operation_id}",
                        data=event["data"],
                    )
                )
            return
        sequence = 0
        terminal = False
        yield ": heartbeat\n\n"
        for raw in state.client.stream_operation_events(operation_id, follow=True):
            event = public_value(decode(raw))
            sequence += 1
            if sequence <= resume_after:
                continue
            event_type = str(event.get("type") or event.get("status") or "operation.event") if isinstance(event, dict) else "operation.event"
            yield encode_sse(
                sse_envelope(
                    event_id=sequence,
                    event_type=event_type,
                    resource=f"{API_PREFIX}/operations/{operation_id}",
                    data=event,
                )
            )
            terminal = isinstance(event, dict) and str(event.get("status") or "").lower() in {
                "completed",
                "failed",
                "cancelled",
                "canceled",
            }
            if terminal:
                return
        if not terminal:
            snapshot = get_operation(operation_id)
            sequence += 1
            yield encode_sse(
                sse_envelope(
                    event_id=sequence,
                    event_type="operation.snapshot",
                    resource=f"{API_PREFIX}/operations/{operation_id}",
                    data=snapshot,
                )
            )

    return StreamingResponse(
        source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
