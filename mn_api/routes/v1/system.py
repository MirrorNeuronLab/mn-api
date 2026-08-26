from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from mn_sdk import RuntimeService

from mn_api import state
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
from mn_api.config import auth_enabled
from mn_api.contracts import API_CONTRACT, API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.operations import start_operation
from mn_api.pagination import page
from mn_api.public import public_value, resource_response
from mn_api.routes import system as legacy_system
from mn_api.schemas import ClusterNodeAddRequest


router = APIRouter(prefix=API_PREFIX)


@router.get("/health", operation_id="get_health", tags=["health"], response_model=HealthResponse)
def health():
    config = state.refresh_config_from_env()
    return {
        "status": "ok",
        "api_contract": API_CONTRACT,
        "auth": "enabled" if auth_enabled(config) else "disabled",
    }


@router.get("/runtime/status", operation_id="get_runtime_status", tags=["runtime"], response_model=ResourceModel)
def runtime_status(timeout: float = 3.0, _principal=Depends(require_auth)):
    return public_value(legacy_system.runtime_status(timeout=timeout, _auth=_principal))


@router.get("/runtime/health", operation_id="get_runtime_health", tags=["runtime"], response_model=ResourceModel)
def runtime_health(timeout: float = 3.0, _principal=Depends(require_auth)):
    return public_value(legacy_system.runtime_health(timeout=timeout, _auth=_principal))


@router.get("/runtime/diagnostics", operation_id="get_runtime_diagnostics", tags=["runtime"], response_model=ResourceModel)
def runtime_diagnostics(timeout: float = 3.0, _principal=Depends(require_auth)):
    return public_value(legacy_system.runtime_doctor(timeout=timeout, _auth=_principal))


@router.get("/runtime/resources", operation_id="get_runtime_resources", tags=["runtime"], response_model=ResourceModel)
def runtime_resources(_principal=Depends(require_auth)):
    return public_value(legacy_system.get_resource(_auth=_principal))


@router.put("/runtime/resources", operation_id="replace_runtime_resources", tags=["runtime"], response_model=ResourceModel)
def replace_runtime_resources(request: RuntimeResources, _principal=Depends(require_auth)):
    return public_value(RuntimeService(state.client).set_resource(request.model_dump(exclude_none=True)))


@router.get("/system/summary", operation_id="get_system_summary", tags=["system"], response_model=ResourceModel)
def system_summary(_principal=Depends(require_auth)):
    return public_value(legacy_system.get_system_summary(_auth=_principal))


@router.get("/metrics", operation_id="get_metrics", tags=["system"], response_model=ResourceModel)
def metrics(_principal=Depends(require_auth)):
    return public_value(legacy_system.get_metrics(_auth=_principal))


@router.get("/nodes", operation_id="list_nodes", tags=["nodes"], response_model=PageResponse)
def nodes(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    summary = legacy_system.get_nodes(_auth=principal)
    if isinstance(summary, dict) and isinstance(summary.get("nodes"), list):
        items = [public_value(item) for item in summary["nodes"] if isinstance(item, dict)]
    else:
        items = []
    return page(
        items,
        route=f"{API_PREFIX}/nodes",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
        sort_key="node_id",
        key=lambda item: str(item.get("node_id") or item.get("node") or item.get("name") or ""),
        identity=lambda item: str(item.get("node_id") or item.get("node") or item.get("name") or ""),
    )


@router.post("/nodes", status_code=status.HTTP_201_CREATED, operation_id="create_node", tags=["nodes"], response_model=ResourceModel)
def create_node(request: NodeCreate, response: Response, _principal=Depends(require_auth)):
    result = legacy_system.add_cluster_node(ClusterNodeAddRequest(**request.model_dump()), _auth=_principal)
    node_id = str(result.get("node_name") or "") if isinstance(result, dict) else ""
    if node_id:
        response.headers["Location"] = f"{API_PREFIX}/nodes/{node_id}"
    return public_value(result)


@router.put("/nodes/{node_id}/drain", status_code=status.HTTP_202_ACCEPTED, operation_id="create_node_drain", tags=["nodes"])
def create_node_drain(node_id: str, request: NodeDrain, _principal=Depends(require_auth)):
    operation = start_operation("drain_node", {"node": node_id, **request.model_dump()})
    return resource_response(
        operation,
        status_code=status.HTTP_202_ACCEPTED,
        location=f"{API_PREFIX}/operations/{operation.get('operation_id', '')}",
    )


@router.delete("/nodes/{node_id}/drain", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_node_drain", tags=["nodes"])
def delete_node_drain(node_id: str, _principal=Depends(require_auth)):
    RuntimeService(state.client).undrain_node(node_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/nodes/{node_id}", operation_id="update_node", tags=["nodes"], response_model=ResourceModel)
def update_node(node_id: str, request: NodeMaintenance, _principal=Depends(require_auth)):
    return RuntimeService(state.client).set_node_maintenance(
        node_id,
        enabled=request.maintenance,
        reason=request.reason,
    )


@router.post(
    "/nodes/{node_id}/reconciliations",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_node_reconciliation",
    tags=["nodes"],
)
def create_node_reconciliation(node_id: str, request: ReconciliationCreate, _principal=Depends(require_auth)):
    operation = start_operation("reconcile_node", {"node": node_id, **request.model_dump()})
    return resource_response(
        operation,
        status_code=status.HTTP_202_ACCEPTED,
        location=f"{API_PREFIX}/operations/{operation.get('operation_id', '')}",
    )
