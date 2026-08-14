from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, status

from mn_api import state
from mn_api.api_models import (
    BlueprintInstallation,
    BlueprintRunCreate,
    BlueprintValidation,
    CleanupCreate,
    PageResponse,
    ResourceModel,
)
from mn_api.blueprints import filter_blueprints_by_category, find_blueprint, load_blueprint_catalog
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.http_semantics import require_if_match
from mn_api.operations import start_operation
from mn_api.pagination import page
from mn_api.public import idempotent_response, public_value, resource_response
from mn_api.routes import blueprints as legacy_blueprints
from mn_api.schemas import BlueprintRunRequest


router = APIRouter(prefix=API_PREFIX)


def _config():
    return state.refresh_config_from_env()


@router.get("/blueprints", operation_id="list_blueprints", tags=["blueprints"], response_model=PageResponse)
def list_blueprints(
    category: str | None = None,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    _repo_root, blueprints = load_blueprint_catalog(_config())
    items = [public_value(item) for item in filter_blueprints_by_category(blueprints, category)]
    return page(
        items,
        route=f"{API_PREFIX}/blueprints",
        principal=principal,
        filters={"category": category},
        page_size=page_size,
        page_token=page_token,
        sort_key="id",
        key=lambda item: str(item.get("id") or item.get("blueprint_id") or ""),
        identity=lambda item: str(item.get("id") or item.get("blueprint_id") or ""),
    )


@router.get("/blueprints/{blueprint_id}", operation_id="get_blueprint", tags=["blueprints"], response_model=ResourceModel)
def get_blueprint(blueprint_id: str, _principal=Depends(require_auth)):
    _repo_root, blueprint = find_blueprint(_config(), blueprint_id)
    return public_value(blueprint)


def _installation(blueprint_id: str) -> dict:
    _repo_root, blueprint = find_blueprint(_config(), blueprint_id)
    installation = blueprint.get("installation") if isinstance(blueprint, dict) else None
    if isinstance(installation, dict):
        return {"blueprint_id": blueprint_id, **public_value(installation)}
    installed = bool(blueprint.get("installed")) if isinstance(blueprint, dict) else False
    return {"blueprint_id": blueprint_id, "status": "installed" if installed else "not_installed"}


@router.get(
    "/blueprints/{blueprint_id}/installation",
    operation_id="get_blueprint_installation",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def get_blueprint_installation(blueprint_id: str, _principal=Depends(require_auth)):
    return resource_response(_installation(blueprint_id), etag=True)


@router.put(
    "/blueprints/{blueprint_id}/installation",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="replace_blueprint_installation",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def replace_blueprint_installation(
    blueprint_id: str,
    request: BlueprintInstallation,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    require_if_match(if_match, _installation(blueprint_id))
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprints/{blueprint_id}/installation",
        key=idempotency_key,
        body=request.model_dump(),
        call=lambda: start_operation("install_blueprint", {"blueprint_id": blueprint_id, "force": request.force}),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )


@router.delete(
    "/blueprints/{blueprint_id}/installation",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="delete_blueprint_installation",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def delete_blueprint_installation(
    blueprint_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    require_if_match(if_match, _installation(blueprint_id))
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprints/{blueprint_id}/installation",
        key=idempotency_key,
        body={},
        call=lambda: start_operation("uninstall_blueprint", {"blueprint_id": blueprint_id}),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )


@router.post(
    "/blueprints/{blueprint_id}/validations",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_blueprint_validation",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def create_blueprint_validation(blueprint_id: str, request: BlueprintValidation | None = None, _principal=Depends(require_auth)):
    repo_root, blueprint = find_blueprint(_config(), blueprint_id)
    result = legacy_blueprints.validate_blueprint_inputs(
        repo_root,
        blueprint,
        config_overrides=(request.config_overrides if request else {}),
    )
    return {"blueprint_id": blueprint_id, "result": public_value(result)}


@router.post(
    "/blueprints/{blueprint_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_run",
    tags=["runs"],
    response_model=ResourceModel,
)
def create_blueprint_run(
    blueprint_id: str,
    request: BlueprintRunCreate | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    payload = request or BlueprintRunCreate()

    def start():
        repo_root, blueprint = find_blueprint(_config(), blueprint_id)
        resolved = legacy_blueprints.resolve_async_blueprint_run_request(
            blueprint_id,
            BlueprintRunRequest(
                job_id=payload.job_id,
                run_id=payload.run_id,
                config_overrides=payload.config_overrides,
                force=payload.force,
                fake_llm=payload.fake_llm,
                fake_skills=payload.fake_skills,
            ),
        )
        run = legacy_blueprints.run_blueprint_record(repo_root, blueprint, resolved)
        if isinstance(run, dict):
            run.setdefault("status", "pending")
        return run

    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprints/{blueprint_id}/runs",
        key=idempotency_key,
        body=payload.model_dump(),
        call=start,
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/runs/{result.get('run_id') or result.get('id')}",
    )


@router.post(
    "/blueprint-catalog-refreshes",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_catalog_refresh",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def create_blueprint_catalog_refresh(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprint-catalog-refreshes",
        key=idempotency_key,
        body={},
        call=lambda: start_operation("refresh_blueprint_catalog", {}),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )


@router.post(
    "/blueprint-cleanups",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_cleanup",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def create_blueprint_cleanup(
    request: CleanupCreate | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    options = (request or CleanupCreate()).model_dump()
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprint-cleanups",
        key=idempotency_key,
        body=options,
        call=lambda: start_operation("cleanup_blueprints", options),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )
