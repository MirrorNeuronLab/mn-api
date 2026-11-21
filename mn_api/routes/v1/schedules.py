"""Creation and management of runtime schedules."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, status

from mn_api.api_models import (
    PageResponse,
    ResourceModel,
    ScheduleCreate,
    ScheduleUpdate,
)
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth
from mn_api.runtime_adapter import call_runtime

router = APIRouter(prefix=API_PREFIX)


@router.post(
    "/jobs/{job_id}/schedules",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_job_schedule",
    tags=["schedules"],
    response_model=ResourceModel,
)
async def create_job_schedule(
    job_id: str,
    request: ScheduleCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_job_schedule", locals())


@router.get("/schedules", operation_id="list_schedules", tags=["schedules"], response_model=PageResponse)
async def list_schedules(owner_node: str = "", _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_schedules", locals())


@router.get("/schedules/{schedule_id}", operation_id="get_schedule", tags=["schedules"], response_model=ResourceModel)
async def get_schedule(schedule_id: str, owner_node: str = "", _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_schedule", locals())


@router.patch(
    "/schedules/{schedule_id}", operation_id="update_schedule", tags=["schedules"], response_model=ResourceModel
)
async def update_schedule(
    schedule_id: str,
    request: ScheduleUpdate,
    owner_node: str = "",
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("update_schedule", locals())


@router.delete("/schedules/{schedule_id}", operation_id="delete_schedule", tags=["schedules"])
async def delete_schedule(
    schedule_id: str,
    owner_node: str = "",
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_schedule", locals())
