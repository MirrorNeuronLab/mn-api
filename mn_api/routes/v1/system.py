"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, Query, Response, status
from mn_api.api_models import (
    HealthResponse,
    NodeCreate,
    NodeDrain,
    NodeMaintenance,
    PageResponse,
    ReconciliationCreate,
    ResourceModel,
    RuntimeResources,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


@router.get("/health", operation_id="get_health", tags=["health"], response_model=HealthResponse)
async def health():
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_health", locals())


@router.get("/runtime/status", operation_id="get_runtime_status", tags=["runtime"], response_model=ResourceModel)
async def runtime_status(timeout: float = 3.0, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_runtime_status", locals())


@router.get("/runtime/health", operation_id="get_runtime_health", tags=["runtime"], response_model=ResourceModel)
async def runtime_health(timeout: float = 3.0, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_runtime_health", locals())


@router.get(
    "/runtime/diagnostics", operation_id="get_runtime_diagnostics", tags=["runtime"], response_model=ResourceModel
)
async def runtime_diagnostics(timeout: float = 3.0, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_runtime_diagnostics", locals())


@router.get("/runtime/resources", operation_id="get_runtime_resources", tags=["runtime"], response_model=ResourceModel)
async def runtime_resources(_principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_runtime_resources", locals())


@router.put(
    "/runtime/resources", operation_id="replace_runtime_resources", tags=["runtime"], response_model=ResourceModel
)
async def replace_runtime_resources(request: RuntimeResources, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("replace_runtime_resources", locals())


@router.get("/system/summary", operation_id="get_system_summary", tags=["system"], response_model=ResourceModel)
async def system_summary(_principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_system_summary", locals())


@router.get("/metrics", operation_id="get_metrics", tags=["system"], response_model=ResourceModel)
async def metrics(_principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_metrics", locals())


@router.get("/nodes", operation_id="list_nodes", tags=["nodes"], response_model=PageResponse)
async def nodes(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_nodes", locals())


@router.post(
    "/nodes",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_node",
    tags=["nodes"],
    response_model=ResourceModel,
)
async def create_node(request: NodeCreate, response: Response, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_node", locals())


@router.delete("/nodes/{node_id}", operation_id="delete_node", tags=["nodes"], response_model=ResourceModel)
async def delete_node(node_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_node", locals())


@router.put(
    "/nodes/{node_id}/drain", status_code=status.HTTP_202_ACCEPTED, operation_id="create_node_drain", tags=["nodes"]
)
async def create_node_drain(node_id: str, request: NodeDrain, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_node_drain", locals())


@router.delete(
    "/nodes/{node_id}/drain", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_node_drain", tags=["nodes"]
)
async def delete_node_drain(node_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_node_drain", locals())


@router.patch("/nodes/{node_id}", operation_id="update_node", tags=["nodes"], response_model=ResourceModel)
async def update_node(node_id: str, request: NodeMaintenance, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("update_node", locals())


@router.post(
    "/nodes/{node_id}/reconciliations",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_node_reconciliation",
    tags=["nodes"],
)
async def create_node_reconciliation(node_id: str, request: ReconciliationCreate, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_node_reconciliation", locals())
