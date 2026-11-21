"""Durable job definitions and launch requests."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, status

from mn_api.api_models import (
    JobBundleReplacement,
    JobCreate,
    JobRelocationCreate,
    JobUpdate,
    PageResponse,
    ResourceModel,
    RunAdmissionCheck,
    RunCreate,
    WorkflowDag,
    WorkflowSteps,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.job_analysis_models import JobAnalysis
from mn_api.runtime_adapter import call_runtime

router = APIRouter(prefix=API_PREFIX)


@router.post(
    "/jobs/{job_id}/relocations",
    status_code=202,
    operation_id="relocate_job",
    tags=["jobs"],
    response_model=ResourceModel,
)
async def relocate_job(job_id: str, request: JobRelocationCreate, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("relocate_job", locals())


@router.post(
    "/jobs", status_code=status.HTTP_201_CREATED, operation_id="create_job", tags=["jobs"], response_model=ResourceModel
)
async def create_job(
    request: JobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
    progress_id: str | None = Header(default=None, alias="X-Launch-Progress-ID", max_length=220),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_job", locals())


@router.get("/jobs", operation_id="list_jobs", tags=["jobs"], response_model=PageResponse)
async def list_jobs(
    include_archived: bool = False,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_jobs", locals())


@router.get("/jobs/{job_id}", operation_id="get_job", tags=["jobs"], response_model=ResourceModel)
async def get_job(job_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job", locals())


@router.get(
    "/jobs/{job_id}/workflow/definition/dag",
    operation_id="get_job_definition_dag",
    tags=["jobs"],
    response_model=WorkflowDag,
)
async def get_job_definition_dag(job_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job_definition_dag", locals())


@router.get(
    "/jobs/{job_id}/workflow/definition/steps",
    operation_id="get_job_definition_steps",
    tags=["jobs"],
    response_model=WorkflowSteps,
)
async def get_job_definition_steps(job_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job_definition_steps", locals())


@router.get(
    "/jobs/{job_id}/workflow/latest-run/dag",
    operation_id="get_job_latest_run_dag",
    tags=["jobs"],
    response_model=WorkflowDag,
)
async def get_job_latest_run_dag(job_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job_latest_run_dag", locals())


@router.get(
    "/jobs/{job_id}/workflow/latest-run/steps",
    operation_id="get_job_latest_run_steps",
    tags=["jobs"],
    response_model=WorkflowSteps,
)
async def get_job_latest_run_steps(job_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job_latest_run_steps", locals())


@router.get("/jobs/{job_id}/ui", operation_id="get_job_ui", tags=["jobs"], response_model=ResourceModel)
async def get_job_ui(job_id: str, http_request: Request, principal=Depends(require_auth), check_readiness: bool = True):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job_ui", locals())


@router.patch("/jobs/{job_id}", operation_id="update_job", tags=["jobs"], response_model=ResourceModel)
async def update_job(
    job_id: str,
    request: JobUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("update_job", locals())


@router.put("/jobs/{job_id}/bundle", operation_id="replace_job_bundle", tags=["jobs"], response_model=ResourceModel)
async def replace_job_bundle(
    job_id: str,
    request: JobBundleReplacement,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("replace_job_bundle", locals())


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_job", tags=["jobs"])
async def delete_job(
    job_id: str, if_match: str | None = Header(default=None, alias="If-Match"), _principal=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_job", locals())


@router.post(
    "/jobs/{job_id}/data-resets",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_job_data_reset",
    tags=["jobs"],
    response_model=ResourceModel,
)
async def create_job_data_reset(
    job_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_job_data_reset", locals())


@router.post(
    "/jobs/{job_id}/run-admission-checks",
    operation_id="check_job_run_admission",
    tags=["runs"],
    response_model=ResourceModel,
)
async def check_job_run_admission(job_id: str, request: RunAdmissionCheck, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("check_job_run_admission", locals())


@router.post(
    "/jobs/{job_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_job_run",
    tags=["runs"],
    response_model=ResourceModel,
)
async def create_job_run(
    job_id: str,
    request: RunCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_job_run", locals())


@router.post(
    "/jobs/{job_id}/run-operations",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_job_run_operation",
    tags=["runs"],
    response_model=ResourceModel,
)
async def create_job_run_operation(
    job_id: str,
    request: RunCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_job_run_operation", locals())


@router.get("/jobs/{job_id}/analysis", operation_id="get_job_analysis", tags=["jobs"], response_model=JobAnalysis)
async def get_job_analysis(job_id: str, request: Request, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_job_analysis", locals())
