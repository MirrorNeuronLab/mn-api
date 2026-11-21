"""Run admission and lifecycle endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, status

from mn_api.api_models import (
    PageResponse,
    ResourceModel,
    RunResource,
    RunRetryCreate,
    RunRetryPlan,
    RunUpdate,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.runtime_adapter import call_runtime

router = APIRouter(prefix=API_PREFIX)


@router.get("/jobs/{job_id}/runs", operation_id="list_job_runs", tags=["runs"], response_model=PageResponse)
async def list_job_runs(
    job_id: str,
    include_terminal: bool = True,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_job_runs", locals())


@router.get("/runs", operation_id="list_runs", tags=["runs"], response_model=PageResponse)
async def list_runs(
    include_terminal: bool = True,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_runs", locals())


@router.get("/runs/{run_id}", operation_id="get_run", tags=["runs"], response_model=RunResource)
async def get_run(run_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run", locals())


@router.patch("/runs/{run_id}", operation_id="update_run", tags=["runs"], response_model=ResourceModel)
async def update_run(
    run_id: str,
    request: RunUpdate,
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("update_run", locals())


@router.post("/runs/{run_id}/retry-plans", operation_id="plan_run_retry", tags=["runs"], response_model=ResourceModel)
async def plan_run_retry(run_id: str, request: RunRetryPlan, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("plan_run_retry", locals())


@router.post(
    "/runs/{run_id}/retries",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="retry_run",
    tags=["runs"],
    response_model=ResourceModel,
)
async def retry_run(
    run_id: str,
    request: RunRetryCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("retry_run", locals())


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_run", tags=["runs"])
async def delete_run(run_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_run", locals())
