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
    cleanup_blueprint_run_processes,
    start_blueprint_pre_launch_hook,
    validate_blueprint_inputs,
    validate_blueprint_bundle,
    validate_run_id,
    write_blueprint_job_mapping,
)
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
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


@router.post("/blueprints/{blueprint_id}/validate")
def validate_blueprint(
    blueprint_id: str,
    req: BlueprintRunRequest | None = None,
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    config_overrides = None
    if req:
        config_overrides = req.config_overwrite or req.config_overrides
    result = validate_blueprint_inputs(
        repo_root,
        blueprint,
        config_overrides=config_overrides,
    )
    return {"blueprint": blueprint, "validation": result}


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
    force = bool(req.force) if req else False
    pre_launch_process = start_blueprint_pre_launch_hook(
        repo_root,
        blueprint,
        run_id,
        config_overrides=config_overrides,
    )
    if not force:
        validation = validate_blueprint_inputs(
            repo_root,
            blueprint,
            config_overrides=config_overrides,
        )
        if not validation.get("ok"):
            cleanup_blueprint_run_processes(run_id)
            return validation_problem_response(
                validation,
                status_code=422,
                error="input_validation_failed",
                title="Blueprint input validation failed",
                detail="Fix the highlighted blueprint input fields, or pass force=true to run anyway.",
                extra={"run_id": run_id, "blueprint": blueprint},
            )
    manifest_json, payloads = load_blueprint_bundle(
        repo_root,
        blueprint,
        run_id,
        config_overrides=config_overrides,
        force=force,
    )

    try:
        if force:
            job_id = state.client.submit_job(manifest_json, payloads, force=True)
        else:
            job_id = state.client.submit_job(manifest_json, payloads)
        write_blueprint_job_mapping(
            run_id,
            job_id,
            blueprint_id=blueprint["id"],
            blueprint_revision=blueprint.get("revision") or None,
        )
        return {
            "job_id": job_id,
            "run_id": run_id,
            "status": "pending",
            "blueprint": blueprint,
        }
    except Exception as exc:
        cleanup_blueprint_run_processes(run_id)
        return handle_grpc_error(exc)
