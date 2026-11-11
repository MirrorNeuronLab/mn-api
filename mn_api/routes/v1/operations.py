"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, Header, Query
from mn_api.api_models import PageResponse, ResourceModel
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


@router.get("/operations", operation_id="list_operations", tags=["operations"], response_model=PageResponse)
async def list_operations(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_operations", locals())


@router.get(
    "/operations/{operation_id}", operation_id="get_operation", tags=["operations"], response_model=ResourceModel
)
async def operation(operation_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_operation", locals())


@router.get("/operations/{operation_id}/events/stream", operation_id="stream_operation_events", tags=["operations"])
async def stream_operation_events(
    operation_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("stream_operation_events", locals())
