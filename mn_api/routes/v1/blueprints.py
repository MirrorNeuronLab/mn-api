"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, Header, Query, status
from mn_api.api_models import (
    BlueprintAdd,
    BlueprintPageResponse,
    BlueprintRemove,
    BlueprintResource,
    BlueprintRunCreate,
    BlueprintValidation,
    CleanupCreate,
    ResourceModel,
)
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


@router.get(
    "/launch-progress/{progress_id}",
    operation_id="get_launch_progress",
    tags=["blueprints"],
    response_model=ResourceModel,
)
async def get_launch_progress(progress_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_launch_progress", locals())


@router.get(
    "/blueprints",
    operation_id="list_blueprints",
    tags=["blueprints"],
    response_model=BlueprintPageResponse,
    response_model_exclude_unset=True,
)
async def list_blueprints(
    category: str | None = None,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_blueprints", locals())


@router.get(
    "/blueprints/{blueprint_id}",
    operation_id="get_blueprint",
    tags=["blueprints"],
    response_model=BlueprintResource,
    response_model_exclude_unset=True,
)
async def get_blueprint(blueprint_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_blueprint", locals())


@router.post(
    "/blueprints/{blueprint_id}/additions",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_addition",
    tags=["blueprints"],
    response_model=ResourceModel,
)
async def create_blueprint_addition(
    blueprint_id: str,
    request: BlueprintAdd | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_blueprint_addition", locals())


@router.post(
    "/blueprints/{blueprint_id}/removals",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_removal",
    tags=["blueprints"],
    response_model=ResourceModel,
)
async def create_blueprint_removal(
    blueprint_id: str,
    request: BlueprintRemove | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_blueprint_removal", locals())


@router.post(
    "/blueprints/{blueprint_id}/validations",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_blueprint_validation",
    tags=["blueprints"],
    response_model=ResourceModel,
)
async def create_blueprint_validation(
    blueprint_id: str, request: BlueprintValidation | None = None, _principal=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_blueprint_validation", locals())


@router.post(
    "/blueprints/{blueprint_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_run",
    tags=["runs"],
    response_model=ResourceModel,
)
async def create_blueprint_run(
    blueprint_id: str,
    request: BlueprintRunCreate | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
    progress_id: str | None = Header(default=None, alias="X-Launch-Progress-ID", max_length=220),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_blueprint_run", locals())


@router.post(
    "/blueprint-catalog-refreshes",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_catalog_refresh",
    tags=["blueprints"],
    response_model=ResourceModel,
)
async def create_blueprint_catalog_refresh(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_blueprint_catalog_refresh", locals())


@router.post(
    "/blueprint-cleanups",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_cleanup",
    tags=["blueprints"],
    response_model=ResourceModel,
)
async def create_blueprint_cleanup(
    request: CleanupCreate | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_blueprint_cleanup", locals())
