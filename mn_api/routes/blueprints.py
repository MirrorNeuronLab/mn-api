from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from mn_api import state
from mn_api.blueprints import (
    create_blueprint_run_id,
    filter_blueprints_by_category,
    find_blueprint,
    load_blueprint_categories,
    load_blueprint_bundle,
    load_blueprint_catalog,
    validate_blueprint_bundle,
    validate_run_id,
)
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import BlueprintRunRequest


router = APIRouter(prefix="/api/v1")


@router.get("/blueprints")
def list_blueprints(
    category: str | None = Query(
        default=None,
        description="Optional blueprint category name or slug. Comma-separated values are allowed.",
    ),
    _auth=Depends(require_auth),
):
    repo_root, blueprints = load_blueprint_catalog(state.config)
    categories = load_blueprint_categories(repo_root, blueprints)
    filtered_blueprints = filter_blueprints_by_category(blueprints, category)
    return {"repo_dir": str(repo_root), "blueprints": filtered_blueprints, "categories": categories}


@router.get("/blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, _auth=Depends(require_auth)):
    _repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    return {"blueprint": blueprint}


@router.post("/blueprints/{blueprint_id}/install")
def install_blueprint(blueprint_id: str, _auth=Depends(require_auth)):
    repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    validate_blueprint_bundle(repo_root, blueprint)
    return {"installed": True, "blueprint": blueprint}


@router.post("/blueprints/{blueprint_id}/runs")
def run_blueprint(
    blueprint_id: str,
    req: BlueprintRunRequest | None = None,
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    run_id = req.run_id if req and req.run_id else create_blueprint_run_id(blueprint_id)
    validate_run_id(run_id)
    config_overrides = None
    if req:
        config_overrides = req.config_overwrite or req.config_overrides
    manifest_json, payloads = load_blueprint_bundle(
        repo_root,
        blueprint,
        run_id,
        config_overrides=config_overrides,
    )

    try:
        job_id = state.client.submit_job(manifest_json, payloads)
        return {
            "job_id": job_id,
            "run_id": run_id,
            "status": "pending",
            "blueprint": blueprint,
        }
    except Exception as exc:
        return handle_grpc_error(exc)
