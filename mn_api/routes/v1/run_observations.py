"""Run progress, events, outputs and human interaction."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, status

from mn_api.api_models import (
    HumanAcknowledgement,
    HumanResponse,
    PageResponse,
    ResourceModel,
    RunResource,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.runtime_adapter import call_runtime

router = APIRouter(prefix=API_PREFIX)


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
