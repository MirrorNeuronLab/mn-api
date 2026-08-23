from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, status

from mn_api import state
from mn_api.api_models import (
    BlueprintAdd,
    BlueprintRemove,
    BlueprintRunCreate,
    BlueprintValidation,
    CleanupCreate,
    PageResponse,
    ResourceModel,
)
from mn_api.blueprint_additions import add_catalog_blueprint, blueprint_public_projection
from mn_api.blueprints import filter_blueprints_by_category, find_blueprint, load_blueprint_catalog, refresh_blueprint_catalog
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.operations import complete_local_operation, start_local_operation, start_operation
from mn_api.pagination import page
from mn_api.public import idempotent_response, public_value
from mn_api.routes import blueprints as legacy_blueprints
from mn_api.schemas import BlueprintRunRequest, BlueprintUninstallRequest


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
    items = [blueprint_public_projection(item) for item in filter_blueprints_by_category(blueprints, category)]
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
    return blueprint_public_projection(blueprint)


@router.post(
    "/blueprints/{blueprint_id}/additions",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_addition",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def create_blueprint_addition(
    blueprint_id: str,
    request: BlueprintAdd | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    payload = request or BlueprintAdd()

    def add():
        return start_local_operation(
            "add_blueprint",
            {"blueprint_id": blueprint_id, "force": payload.force},
            lambda report: add_catalog_blueprint(
                _config(),
                blueprint_id,
                force=payload.force,
                report_progress=report,
            ),
        )

    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprints/{blueprint_id}/additions",
        key=idempotency_key,
        body=payload.model_dump(),
        call=add,
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )


@router.post(
    "/blueprints/{blueprint_id}/removals",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_blueprint_removal",
    tags=["blueprints"],
    response_model=ResourceModel,
)
def create_blueprint_removal(
    blueprint_id: str,
    request: BlueprintRemove | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    payload = request or BlueprintRemove()

    def remove(report):
        report(percent=20, stage="resolve_blueprint", label="Resolve blueprint", detail="Locating the added blueprint record.")
        report(percent=55, stage="remove_resources", label="Remove resources", detail="Removing blueprint-owned runtime resources.")
        result = legacy_blueprints.uninstall_blueprints(
            BlueprintUninstallRequest(blueprint_id=blueprint_id, **payload.model_dump()),
            _auth=principal,
        )
        report(percent=90, stage="record_removal", label="Record removal", detail="Finalizing the blueprint removal.")
        return result

    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprints/{blueprint_id}/removals",
        key=idempotency_key,
        body=payload.model_dump(),
        call=lambda: start_local_operation("remove_blueprint", {"blueprint_id": blueprint_id, **payload.model_dump()}, remove),
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
                secret_environment=payload.secret_environment,
                force=payload.force,
                fake_llm=payload.fake_llm,
                fake_skills=payload.fake_skills,
                owner_node=payload.owner_node,
                replace_existing_run=payload.replace_existing_run,
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
    def refresh():
        repo_root, blueprints = refresh_blueprint_catalog(_config())
        return complete_local_operation(
            "refresh_blueprint_catalog",
            result={"repo_root": str(repo_root), "blueprint_count": len(blueprints)},
        )

    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/blueprint-catalog-refreshes",
        key=idempotency_key,
        body={},
        call=refresh,
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
