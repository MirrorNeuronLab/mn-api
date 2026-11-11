"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, Header
from mn_api.api_models import JobCreate, JobUpdate, ResourceModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


@router.post(
    "/job-preparations", status_code=202, operation_id="prepare_job", tags=["jobs"], response_model=ResourceModel
)
async def prepare_job(
    request: JobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("prepare_job", locals())


@router.post(
    "/jobs/{job_id}/preparations",
    status_code=202,
    operation_id="prepare_job_update",
    tags=["jobs"],
    response_model=ResourceModel,
)
async def prepare_job_update(
    job_id: str,
    request: JobUpdate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("prepare_job_update", locals())
