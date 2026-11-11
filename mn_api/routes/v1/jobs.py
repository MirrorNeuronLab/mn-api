"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, Header, Query, Request, status
from mn_api.job_analysis_models import JobAnalysis
from mn_api.api_models import (
    HumanAcknowledgement,
    HumanResponse,
    JobBundleReplacement,
    JobCreate,
    JobUpdate,
    JobRelocationCreate,
    PageResponse,
    ResourceModel,
    RunCreate,
    RunResource,
    RunAdmissionCheck,
    RunUpdate,
    RunRetryPlan,
    RunRetryCreate,
    ScheduleCreate,
    ScheduleUpdate,
    WorkflowDag,
    WorkflowSteps,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth

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


@router.get("/runs/{run_id}/monitor", operation_id="get_run_monitor", tags=["runs"], response_model=RunResource)
async def get_run_monitor(run_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_monitor", locals())


@router.get(
    "/runs/{run_id}/workflow-progress",
    operation_id="get_run_workflow_progress",
    tags=["runs"],
    response_model=ResourceModel,
)
async def get_run_workflow_progress(run_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_workflow_progress", locals())


@router.get("/runs/{run_id}/logs", operation_id="list_run_logs", tags=["runs"], response_model=PageResponse)
async def list_run_logs(
    run_id: str,
    level: str | None = None,
    since: str | None = None,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_run_logs", locals())


@router.get("/runs/{run_id}/events", operation_id="list_run_events", tags=["runs"], response_model=PageResponse)
async def list_run_events(
    run_id: str,
    channel: str | None = None,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_run_events", locals())


@router.get("/runs/{run_id}/events/stream", operation_id="stream_run_events", tags=["runs"])
async def stream_run_events(
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    interval: float = Query(1.0, ge=0.25, le=30.0),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("stream_run_events", locals())


@router.get("/runs/{run_id}/resources", operation_id="get_run_resources", tags=["runs"], response_model=ResourceModel)
async def get_run_resources(run_id: str, window: str = "24h", bucket: str = "1h", principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_resources", locals())


@router.get(
    "/runs/{run_id}/human-requests", operation_id="list_run_human_requests", tags=["runs"], response_model=PageResponse
)
async def list_run_human_requests(
    run_id: str,
    request_status: str | None = Query(default=None, alias="status"),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_run_human_requests", locals())


@router.post(
    "/runs/{run_id}/human-requests/{request_id}/responses",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_run_human_response",
    tags=["runs"],
    response_model=ResourceModel,
)
async def create_run_human_response(
    run_id: str, request_id: str, request: HumanResponse, principal=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_run_human_response", locals())


@router.post(
    "/runs/{run_id}/human-requests/{request_id}/acknowledgements",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_run_human_acknowledgement",
    tags=["runs"],
    response_model=ResourceModel,
)
async def create_run_human_acknowledgement(
    run_id: str, request_id: str, request: HumanAcknowledgement, principal=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_run_human_acknowledgement", locals())


@router.get(
    "/runs/{run_id}/artifacts/final", operation_id="get_run_final_artifact", tags=["runs"], response_model=ResourceModel
)
async def get_run_final_artifact(run_id: str, request: Request, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_final_artifact", locals())


@router.get("/runs/{run_id}/artifacts", operation_id="list_run_artifacts", tags=["runs"], response_model=PageResponse)
async def list_run_artifacts(
    run_id: str,
    request: Request,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_run_artifacts", locals())


@router.get("/runs/{run_id}/artifacts/{artifact_path:path}", operation_id="download_run_artifact", tags=["runs"])
async def download_run_artifact(run_id: str, artifact_path: str, request: Request, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("download_run_artifact", locals())


@router.get("/runs/{run_id}/outputs", operation_id="list_run_outputs", tags=["runs"], response_model=PageResponse)
async def list_run_outputs(
    run_id: str,
    request: Request,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_run_outputs", locals())


@router.get("/runs/{run_id}/outputs/{output_index}", operation_id="download_run_output", tags=["runs"])
async def download_run_output(run_id: str, output_index: int, request: Request, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("download_run_output", locals())


@router.get(
    "/runs/{run_id}/observability", operation_id="get_run_observability", tags=["runs"], response_model=ResourceModel
)
async def get_run_observability(run_id: str, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_observability", locals())


@router.get("/runs/{run_id}/snapshots", operation_id="get_run_snapshot", tags=["runs"], response_model=ResourceModel)
async def get_run_snapshot(run_id: str, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_snapshot", locals())


@router.get(
    "/runs/{run_id}/agent-graph", operation_id="get_run_agent_graph", tags=["runs"], response_model=ResourceModel
)
async def get_run_agent_graph(run_id: str, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_run_agent_graph", locals())


@router.get("/runs/{run_id}/export", operation_id="export_run", tags=["runs"])
async def export_run(run_id: str, format: str = "json", principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("export_run", locals())
