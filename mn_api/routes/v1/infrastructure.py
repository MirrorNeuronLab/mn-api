from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from mn_sdk import RuntimeService, deployment_policy
from mn_sdk import load_model_proxies, remove_model_proxy, remove_model_remote, upsert_model_proxy, upsert_model_remote

from mn_api import state
from mn_api.api_models import (
    DeploymentCreate,
    DeploymentRollback,
    DesiredStateUpdate,
    ModelInstallation,
    ModelBenchmark,
    ModelProxyRegistration,
    ModelRegistration,
    PageResponse,
    ResourceModel,
    ServiceCheck,
)
from mn_api.blueprints import (
    blueprint_runtime_environment,
    expand_blueprint_manifest_if_source,
    load_blueprint_config,
)
from mn_api.bundles import load_uploaded_bundle, uploaded_bundle_root
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.http_semantics import require_if_match
from mn_api.operations import start_operation
from mn_api.pagination import page
from mn_api.public import idempotent_response, public_value, records, resource_response
from mn_api.routes import models as model_routes
from mn_sdk import run_service_validation


router = APIRouter(prefix=API_PREFIX)


def _service() -> RuntimeService:
    return RuntimeService(state.client)


def _page(
    items: list[dict[str, Any]],
    *,
    route: str,
    principal: str,
    filters: dict[str, Any],
    page_size: int,
    page_token: str | None,
    id_names: tuple[str, ...],
):
    def identity(item: dict[str, Any]) -> str:
        for name in id_names:
            if item.get(name):
                return str(item[name])
        return ""

    return page(
        items,
        route=route,
        principal=principal,
        filters=filters,
        page_size=page_size,
        page_token=page_token,
        sort_key=id_names[0],
        key=identity,
        identity=identity,
    )


@router.post(
    "/deployments",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_deployment",
    tags=["deployments"],
    response_model=ResourceModel,
)
def create_deployment(request: DeploymentCreate, response: Response, _principal=Depends(require_auth)):
    manifest_json, payloads = load_uploaded_bundle(request.bundle_id, state.BUNDLE_UPLOAD_ROOT)
    policy = deployment_policy(
        str(request.policy.get("strategy") or "rolling"),
        int(request.policy.get("canary") or 0),
        int(request.policy.get("max_parallel") or 1),
        bool(request.policy.get("auto_promote")),
        bool(request.policy.get("auto_revert")),
    )
    deployment = _service().deploy_job(
        manifest_json,
        payloads,
        deployment_key=request.deployment_key,
        policy=policy,
        wait=request.wait,
    )
    deployment_id = str(deployment.get("deployment_id") or deployment.get("id") or request.deployment_key)
    response.headers["Location"] = f"{API_PREFIX}/deployments/{deployment_id}"
    response.headers["ETag"] = resource_response(deployment, etag=True).headers["etag"]
    return public_value(deployment)


@router.get("/deployments", operation_id="list_deployments", tags=["deployments"], response_model=PageResponse)
def list_deployments(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    return _page(
        records(_service().list_deployments(), "items", "deployments", "data"),
        route=f"{API_PREFIX}/deployments",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
        id_names=("deployment_id", "id", "deployment_key"),
    )


@router.get(
    "/deployments/{deployment_id}", operation_id="get_deployment", tags=["deployments"], response_model=ResourceModel
)
def get_deployment(deployment_id: str, _principal=Depends(require_auth)):
    return resource_response(_service().get_deployment(deployment_id), etag=True)


@router.patch(
    "/deployments/{deployment_id}", operation_id="update_deployment", tags=["deployments"], response_model=ResourceModel
)
def update_deployment(
    deployment_id: str,
    request: DesiredStateUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_deployment(deployment_id))
    require_if_match(if_match, current)
    if request.desired_state == "running":
        result = _service().resume_deployment(deployment_id, reason=request.reason)
    elif request.desired_state == "paused":
        result = _service().pause_deployment(deployment_id, reason=request.reason)
    elif request.desired_state == "failed":
        result = _service().fail_deployment(deployment_id, reason=request.reason)
    else:
        result = start_operation("cancel_deployment", {"deployment_id": deployment_id, "reason": request.reason})
    return resource_response(result, etag=True)


@router.delete("/deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_deployment", tags=["deployments"])
def delete_deployment(
    deployment_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_deployment(deployment_id))
    require_if_match(if_match, current)
    start_operation("delete_deployment", {"deployment_id": deployment_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/deployments/{deployment_id}/promotions",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_deployment_promotion",
    tags=["deployments"],
    response_model=ResourceModel,
)
def create_deployment_promotion(deployment_id: str, _principal=Depends(require_auth)):
    return public_value(_service().promote_deployment(deployment_id))


@router.post(
    "/deployments/{deployment_id}/rollbacks",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_deployment_rollback",
    tags=["deployments"],
    response_model=ResourceModel,
)
def create_deployment_rollback(deployment_id: str, request: DeploymentRollback, _principal=Depends(require_auth)):
    result = _service().rollback_deployment(
        deployment_id,
        version=request.target_version or "",
        tag=request.tag,
        reason=request.reason,
    )
    return resource_response(result, status_code=status.HTTP_202_ACCEPTED, location=f"{API_PREFIX}/deployments/{deployment_id}")


@router.get("/models", operation_id="list_models", tags=["models"], response_model=PageResponse)
def list_models(
    installed_only: bool = True,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    items = records(model_routes.list_runtime_models(installed_only=installed_only), "items", "models", "data")
    return _page(
        items,
        route=f"{API_PREFIX}/models",
        principal=principal,
        filters={"installed_only": installed_only},
        page_size=page_size,
        page_token=page_token,
        id_names=("id", "model_id", "name"),
    )


@router.get("/models/{model_id:path}", operation_id="get_model", tags=["models"], response_model=ResourceModel)
def get_model(model_id: str, _principal=Depends(require_auth)):
    return public_value(model_routes.show_runtime_model(model_id))


@router.put(
    "/models/{model_id:path}/installation",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="replace_model_installation",
    tags=["models"],
    response_model=ResourceModel,
)
def replace_model_installation(
    model_id: str,
    request: ModelInstallation,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/models/{model_id}/installation",
        key=idempotency_key,
        body=request.model_dump(),
        call=lambda: start_operation("install_model", {"model_id": model_id, **request.model_dump()}),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )


@router.delete(
    "/models/{model_id:path}/installation",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="delete_model_installation",
    tags=["models"],
    response_model=ResourceModel,
)
def delete_model_installation(model_id: str, principal=Depends(require_auth)):
    operation = start_operation("remove_model", {"model_id": model_id})
    return resource_response(
        operation,
        status_code=status.HTTP_202_ACCEPTED,
        location=f"{API_PREFIX}/operations/{operation.get('operation_id') or operation.get('id')}",
    )


@router.post(
    "/models/{model_id:path}/benchmarks",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_model_benchmark",
    tags=["models"],
    response_model=ResourceModel,
)
def create_model_benchmark(model_id: str, request: ModelBenchmark | None = None, principal=Depends(require_auth)):
    return public_value(model_routes.benchmark_model(model_id, (request or ModelBenchmark()).model_dump(), principal))


@router.get("/model-remotes", operation_id="list_model_remotes", tags=["models"], response_model=PageResponse)
def list_model_remotes(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal=Depends(require_auth),
):
    ledger = model_routes.load_model_remotes()
    items = [public_value(item) for item in (ledger.get("remotes") or {}).values()]
    return _page(
        items,
        route=f"{API_PREFIX}/model-remotes",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
        id_names=("name", "model", "id"),
    )


@router.post(
    "/model-remotes",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_model_remote",
    tags=["models"],
    response_model=ResourceModel,
)
def create_model_remote(request: ModelRegistration, response: Response, principal=Depends(require_auth)):
    remote = upsert_model_remote(
        request.model,
        request.base_url,
        name=request.name,
        api_model=request.api_model,
        api_key="not-needed",
        node=request.node,
    )
    name = str(remote.get("name") or request.name or request.model)
    response.headers["Location"] = f"{API_PREFIX}/model-remotes/{name}"
    response.headers["ETag"] = resource_response(remote, etag=True).headers["etag"]
    return public_value(remote)


@router.delete("/model-remotes/{name}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_model_remote", tags=["models"])
def delete_model_remote(
    name: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal=Depends(require_auth),
):
    current = next(
        (
            item
            for item in list_model_remotes(page_size=MAX_PAGE_SIZE, principal=principal)["items"]
            if str(item.get("name") or item.get("model")) == name
        ),
        {"name": name},
    )
    require_if_match(if_match, current)
    remove_model_remote(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/model-proxies",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_model_proxy",
    tags=["models"],
    response_model=ResourceModel,
)
def create_model_proxy(request: ModelProxyRegistration, response: Response, principal=Depends(require_auth)):
    proxy = upsert_model_proxy(
        request.model_id,
        source_model=request.source_model,
        base_url=request.base_url,
        api_model=request.api_model,
        display_name=request.display_name,
        api_key="not-needed",
        container_name=request.container_name,
        image=request.image,
        port=request.port,
        host=request.host,
    )
    response.headers["Location"] = f"{API_PREFIX}/model-proxies/{request.model_id}"
    return public_value(proxy)


@router.get("/model-proxies", operation_id="list_model_proxies", tags=["models"], response_model=PageResponse)
def list_model_proxies(
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal=Depends(require_auth),
):
    ledger = load_model_proxies()
    items = [public_value(item) for item in (ledger.get("proxies") or {}).values()]
    return _page(
        items,
        route=f"{API_PREFIX}/model-proxies",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
        id_names=("model_id", "name", "id"),
    )


@router.delete(
    "/model-proxies/{model_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_model_proxy",
    tags=["models"],
)
def delete_model_proxy(
    model_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal=Depends(require_auth),
):
    current = next(
        (
            item
            for item in list_model_proxies(page_size=MAX_PAGE_SIZE, principal=principal)["items"]
            if str(item.get("model_id") or item.get("name") or item.get("id")) == model_id
        ),
        {"model_id": model_id},
    )
    require_if_match(if_match, current)
    remove_model_proxy(model_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/services", operation_id="list_services", tags=["services"], response_model=PageResponse)
def list_services(
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
    filters = {
        "name": name,
        "node": node,
        "job_id": job_id,
        "agent_id": agent_id,
        "status": service_status,
        "tag": tag or [],
        "passing_only": passing_only,
    }
    value = _service().list_services(
        name=name,
        node=node,
        job_id=job_id,
        agent_id=agent_id,
        status=service_status,
        tags=tag or [],
        passing_only=passing_only,
    )
    return _page(
        records(value, "items", "services", "data"),
        route=f"{API_PREFIX}/services",
        principal=principal,
        filters=filters,
        page_size=page_size,
        page_token=page_token,
        id_names=("id", "service_id", "name"),
    )


@router.get(
    "/services/{name}/resolution",
    operation_id="get_service_resolution",
    tags=["services"],
    response_model=ResourceModel,
)
def get_service_resolution(
    name: str,
    node: str | None = None,
    job_id: str | None = None,
    agent_id: str | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = True,
    principal=Depends(require_auth),
):
    resolved = public_value(
        _service().resolve_service(
            name,
            node=node,
            job_id=job_id,
            agent_id=agent_id,
            tags=tag or [],
            passing_only=passing_only,
        )
    )
    matches = records(resolved, "items", "services", "data")
    return matches[0] if matches else resolved


@router.post(
    "/service-checks",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_service_check",
    tags=["services"],
    response_model=ResourceModel,
)
def create_service_check(request: ServiceCheck, principal=Depends(require_auth)):
    bundle_dir = uploaded_bundle_root(request.bundle_id, state.BUNDLE_UPLOAD_ROOT)
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest = expand_blueprint_manifest_if_source(bundle_dir, manifest)
    config = load_blueprint_config(bundle_dir, config_overrides=request.config_overrides)
    env = blueprint_runtime_environment(bundle_dir, config=config, config_overrides=request.config_overrides)

    def resolver(name: str, requirement: dict):
        result = _service().resolve_service(name, tags=requirement.get("tags") or [], passing_only=True)
        return records(result, "items", "services", "data")

    return public_value(run_service_validation(bundle_dir, manifest, config=config, env=env, resolver=resolver))
