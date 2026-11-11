"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from typing import Annotated
from fastapi import APIRouter, Depends, Header, Query, Response, status
from mn_api.api_models import (
    ModelBenchmark,
    ModelInstallation,
    ModelProxyRegistration,
    ModelRegistration,
    PageResponse,
    ResourceModel,
    ServiceCheck,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


@router.get("/models", operation_id="list_models", tags=["models"], response_model=PageResponse)
async def list_models(
    installed_only: bool = False,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_models", locals())


@router.get("/models/{model_id:path}", operation_id="get_model", tags=["models"], response_model=ResourceModel)
async def get_model(model_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_model", locals())


@router.put(
    "/models/{model_id:path}/installation",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="replace_model_installation",
    tags=["models"],
    response_model=ResourceModel,
)
async def replace_model_installation(
    model_id: str,
    request: ModelInstallation,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("replace_model_installation", locals())


@router.delete(
    "/models/{model_id:path}/installation",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="delete_model_installation",
    tags=["models"],
    response_model=ResourceModel,
)
async def delete_model_installation(model_id: str, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_model_installation", locals())


@router.post(
    "/models/{model_id:path}/benchmarks",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_model_benchmark",
    tags=["models"],
    response_model=ResourceModel,
)
async def create_model_benchmark(model_id: str, request: ModelBenchmark | None = None, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_model_benchmark", locals())


@router.get("/model-remotes", operation_id="list_model_remotes", tags=["models"], response_model=PageResponse)
async def list_model_remotes(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_model_remotes", locals())


@router.post(
    "/model-remotes",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_model_remote",
    tags=["models"],
    response_model=ResourceModel,
)
async def create_model_remote(request: ModelRegistration, response: Response, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_model_remote", locals())


@router.delete(
    "/model-remotes/{name}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_model_remote", tags=["models"]
)
async def delete_model_remote(
    name: str, if_match: str | None = Header(default=None, alias="If-Match"), principal=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_model_remote", locals())


@router.post(
    "/model-proxies",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_model_proxy",
    tags=["models"],
    response_model=ResourceModel,
)
async def create_model_proxy(request: ModelProxyRegistration, response: Response, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_model_proxy", locals())


@router.get("/model-proxies", operation_id="list_model_proxies", tags=["models"], response_model=PageResponse)
async def list_model_proxies(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_model_proxies", locals())


@router.delete(
    "/model-proxies/{model_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_model_proxy",
    tags=["models"],
)
async def delete_model_proxy(
    model_id: str, if_match: str | None = Header(default=None, alias="If-Match"), principal=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("delete_model_proxy", locals())


@router.get("/services", operation_id="list_services", tags=["services"], response_model=PageResponse)
async def list_services(
    name: str | None = None,
    node: str | None = None,
    job_id: str | None = None,
    agent_id: str | None = None,
    service_status: str | None = Query(default=None, alias="status"),
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = True,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_services", locals())


@router.get(
    "/services/{name}/resolution",
    operation_id="get_service_resolution",
    tags=["services"],
    response_model=ResourceModel,
)
async def get_service_resolution(
    name: str,
    node: str | None = None,
    job_id: str | None = None,
    agent_id: str | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = True,
    principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_service_resolution", locals())


@router.post(
    "/service-checks",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_service_check",
    tags=["services"],
    response_model=ResourceModel,
)
async def create_service_check(request: ServiceCheck, principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_service_check", locals())
