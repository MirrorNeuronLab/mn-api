from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from mn_api import state
from mn_api.blueprints import (
    blueprint_runtime_environment,
    expand_blueprint_manifest_if_source,
    load_blueprint_config,
)
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.routes.client_json import client_json_response
from mn_api.schemas import ServiceCheckRequest
from mn_sdk import run_service_validation


router = APIRouter(prefix="/api/v2")


@router.get("/services")
def list_services(
    name: str | None = Query(default=None),
    node: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = Query(default=True),
    _auth=Depends(require_auth),
):
    return client_json_response(
        lambda: state.client.list_services(
            name=name,
            node=node,
            job_id=job_id,
            agent_id=agent_id,
            status=status,
            tags=tag or [],
            passing_only=passing_only,
        )
    )


@router.get("/services/{name}/resolve")
def resolve_service(
    name: str,
    node: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = Query(default=True),
    _auth=Depends(require_auth),
):
    return client_json_response(
        lambda: state.client.resolve_service(
            name,
            node=node,
            job_id=job_id,
            agent_id=agent_id,
            tags=tag or [],
            passing_only=passing_only,
        )
    )


@router.post("/services:check", operation_id="check_services_colon_alias")
@router.post("/services/check", operation_id="check_services_path_alias")
def check_services(req: ServiceCheckRequest, _auth=Depends(require_auth)):
    bundle_dir = _service_check_bundle_dir(req)
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=400, detail="bundle manifest.json not found")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="bundle manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="bundle manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_dir, manifest)
    config = load_blueprint_config(bundle_dir, config_overrides=req.config_overrides)
    env = blueprint_runtime_environment(bundle_dir, config=config, config_overrides=req.config_overrides)

    def resolver(name: str, requirement: dict):
        response = state.client.resolve_service(
            name,
            tags=requirement.get("tags") or [],
            passing_only=True,
        )
        decoded = json.loads(response) if isinstance(response, str) else response
        services = decoded.get("services") if isinstance(decoded, dict) else []
        return services if isinstance(services, list) else []

    return run_service_validation(bundle_dir, manifest, config=config, env=env, resolver=resolver)


def _service_check_bundle_dir(req: ServiceCheckRequest) -> Path:
    if req.bundle_path:
        manifest_json, _payloads = load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
        try:
            decoded = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="uploaded bundle manifest.json is malformed") from exc
        if not isinstance(decoded, dict):
            raise HTTPException(status_code=400, detail="uploaded bundle manifest.json must be an object")
        return Path(req.bundle_path).expanduser().resolve()
    if req.path:
        bundle_dir = Path(req.path).expanduser().resolve()
        if not bundle_dir.is_dir():
            raise HTTPException(status_code=400, detail="bundle path not found")
        return bundle_dir
    raise HTTPException(status_code=422, detail="path or _bundle_path is required")
