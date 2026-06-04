from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from mn_api import state
from mn_api.blueprints import (
    active_job_ids_from_jobs_payload,
    create_blueprint_run_id,
    filter_blueprints_by_category,
    find_blueprint,
    load_blueprint_categories,
    load_blueprint_bundle,
    load_blueprint_catalog,
    cleanup_blueprint_run_processes,
    cleanup_stale_blueprint_run_processes,
    runtime_blueprint_environment_overrides,
    runtime_web_ui_service_from_manifest,
    scheduler_allocated_ports_from_jobs_payload,
    start_background_event_relay_if_needed,
    start_blueprint_pre_launch_hook,
    local_blueprint_from_path,
    run_mn_blueprint_run,
    run_mn_blueprint_validate,
    sanitize_blueprint_id,
    validate_blueprint_inputs,
    validate_blueprint_bundle,
    validate_run_id,
    write_blueprint_job_mapping,
)
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
from mn_api.schemas import BlueprintLaunchRequest, BlueprintRunRequest


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


@router.post("/blueprints/launch/validate")
def validate_blueprint_launch(req: BlueprintLaunchRequest, _auth=Depends(require_auth)):
    launch = resolve_launch_source(req)
    state.close_client()
    validation = run_mn_blueprint_validate(launch["bundle_root"])
    response = {
        "source": launch["source"],
        "blueprint": launch["blueprint"],
        "validation": validation,
        "manifest": launch.get("manifest") or {},
    }
    return response


@router.post("/blueprints/{blueprint_id}/validate")
def validate_blueprint(
    blueprint_id: str,
    req: BlueprintRunRequest | None = None,
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    config_overrides = {}
    if req:
        config_overrides = dict(req.config_overwrite or req.config_overrides or {})
    state.close_client()
    result = validate_blueprint_inputs(
        repo_root,
        blueprint,
        config_overrides=config_overrides,
    )
    return {"blueprint": blueprint, "validation": result}


@router.post("/blueprints/launch/runs")
def run_blueprint_launch(req: BlueprintLaunchRequest, _auth=Depends(require_auth)):
    launch = resolve_launch_source(req)
    force = bool(req.force)
    state.close_client()
    validation = {"ok": True, "status": "skipped" if force else "passed", "issues": [], "errors": []}
    if not force:
        validation = run_mn_blueprint_validate(launch["bundle_root"])
        if not validation.get("ok"):
            return validation_problem_response(
                validation,
                status_code=422,
                error="blueprint_validation_failed",
                title="Blueprint validation failed",
                detail="Fix the highlighted blueprint validation issues and launch again.",
                extra={"blueprint": launch["blueprint"], "source": launch["source"]},
            )

    run_result = run_launch_with_mn_cli(launch, req)
    if not run_result.get("ok"):
        raise HTTPException(status_code=500, detail=run_result.get("error") or "mn blueprint run failed")
    job_id = run_result.get("job_id")
    if not job_id:
        raise HTTPException(status_code=500, detail="mn blueprint run did not report a Job ID")
    return {
        "job_id": job_id,
        "id": job_id,
        "run_id": run_result.get("run_id"),
        "status": "pending",
        "source": launch["source"],
        "blueprint": launch["blueprint"],
        "validation": validation,
        "command": run_result.get("command"),
    }


@router.post("/blueprints/{blueprint_id}/runs")
def run_blueprint(
    blueprint_id: str,
    req: BlueprintRunRequest | None = None,
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    return run_blueprint_record(repo_root, blueprint, req)


def run_blueprint_record(
    repo_root,
    blueprint: dict,
    req: BlueprintRunRequest | None = None,
    *,
    validation: dict | None = None,
):
    blueprint_id = blueprint["id"]
    run_id = req.run_id if req and req.run_id else create_blueprint_run_id(blueprint_id)
    validate_run_id(run_id)
    config_overrides = {}
    if req:
        config_overrides = dict(req.config_overwrite or req.config_overrides or {})
    env_overrides = runtime_blueprint_environment_overrides()
    force = bool(req.force) if req else False
    state.close_client()
    cleanup_stale_blueprint_run_processes(
        repo_root,
        blueprint,
        keep_run_id=run_id,
        active_job_ids=runtime_active_job_ids(),
        reason="stale_blueprint_start",
    )
    start_blueprint_pre_launch_hook(
        repo_root,
        blueprint,
        run_id,
        config_overrides=config_overrides,
        env_overrides=env_overrides,
    )
    if not force:
        validation = validate_blueprint_inputs(
            repo_root,
            blueprint,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
        )
        if not validation.get("ok"):
            cleanup_blueprint_run_processes(run_id, reason="validation_failed")
            return validation_problem_response(
                validation,
                status_code=422,
                error="input_validation_failed",
                title="Blueprint input validation failed",
                detail="Fix the highlighted blueprint input fields, or pass force=true to run anyway.",
                extra={"run_id": run_id, "blueprint": blueprint},
            )
    try:
        manifest_json, payloads = load_blueprint_bundle(
            repo_root,
            blueprint,
            run_id,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
            force=force,
            web_ui_reserved_ports=runtime_blueprint_web_ui_reserved_ports(),
        )
    except HTTPException:
        cleanup_blueprint_run_processes(run_id, reason="manifest_prepare_failed")
        raise

    try:
        if force:
            job_id = state.client.submit_job(manifest_json, payloads, force=True)
        else:
            job_id = state.client.submit_job(manifest_json, payloads)
        submitted_manifest = json.loads(manifest_json)
        web_ui_service = runtime_web_ui_service_from_manifest(submitted_manifest)
        write_blueprint_job_mapping(
            run_id,
            job_id,
            blueprint_id=blueprint["id"],
            blueprint_revision=blueprint.get("revision") or None,
            web_ui_service=web_ui_service,
        )
        start_background_event_relay_if_needed(
            repo_root,
            blueprint,
            run_id,
            job_id,
            manifest_json,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
            grpc_target=getattr(state.config, "grpc_target", None),
            grpc_auth_token=getattr(state.config, "grpc_auth_token", None),
            grpc_timeout_seconds=getattr(state.config, "grpc_timeout_seconds", None),
        )
        return {
            "job_id": job_id,
            "id": job_id,
            "run_id": run_id,
            "status": "pending",
            "blueprint": blueprint,
            "validation": validation,
            "web_ui_service": web_ui_service or None,
        }
    except Exception as exc:
        cleanup_blueprint_run_processes(run_id, reason="launch_failed")
        return handle_grpc_error(exc)


def resolve_launch_source(req: BlueprintLaunchRequest) -> dict:
    source = str(req.source or "").strip().lower().replace("_", "-")
    if source in {"catalog", "blueprint"}:
        if not req.blueprint_id:
            raise HTTPException(status_code=422, detail="blueprint_id is required")
        repo_root, blueprint = find_blueprint(state.config, req.blueprint_id)
        bundle_root = validate_blueprint_bundle(repo_root, blueprint)
        return {
            "source": "catalog",
            "repo_root": repo_root,
            "blueprint": blueprint,
            "bundle_root": bundle_root,
            "manifest": read_manifest_for_launch(bundle_root),
        }

    if source in {"path", "filesystem", "filesystem-path", "local"}:
        if not req.path:
            raise HTTPException(status_code=422, detail="path is required")
        repo_root, blueprint = local_blueprint_from_path(req.path)
        bundle_root = validate_blueprint_bundle(repo_root, blueprint)
        return {
            "source": "path",
            "repo_root": repo_root,
            "blueprint": blueprint,
            "bundle_root": bundle_root,
            "manifest": read_manifest_for_launch(bundle_root),
        }

    if source in {"bundle", "zip", "upload", "uploaded"}:
        if not req.bundle_path:
            raise HTTPException(status_code=422, detail="_bundle_path is required")
        manifest_json, payloads = load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
        manifest = json.loads(manifest_json)
        bundle_root = Path(req.bundle_path).expanduser().resolve()
        blueprint_id = sanitize_blueprint_id(manifest.get("graph_id") or bundle_root.name, "uploaded_bundle")
        blueprint = {
            "id": blueprint_id,
            "name": manifest.get("job_name") or blueprint_id,
            "path": str(bundle_root),
            "source": "uploaded_bundle",
        }
        return {
            "source": "bundle",
            "repo_root": bundle_root.parent,
            "blueprint": blueprint,
            "bundle_root": bundle_root,
            "manifest": manifest,
            "manifest_json": manifest_json,
            "payloads": payloads,
        }

    raise HTTPException(status_code=422, detail="source must be catalog, path, or bundle")


def read_manifest_for_launch(bundle_root: Path) -> dict:
    try:
        manifest = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def submit_uploaded_bundle_launch(launch: dict, req: BlueprintLaunchRequest, validation: dict):
    run_result = run_launch_with_mn_cli(launch, req)
    if not run_result.get("ok"):
        raise HTTPException(status_code=500, detail=run_result.get("error") or "mn blueprint run failed")
    return {
        "job_id": run_result.get("job_id"),
        "id": run_result.get("job_id"),
        "run_id": run_result.get("run_id"),
        "status": "pending",
        "blueprint": launch["blueprint"],
        "validation": validation,
        "command": run_result.get("command"),
    }


def run_launch_with_mn_cli(launch: dict, req: BlueprintLaunchRequest) -> dict:
    run_args = ["--folder", str(launch["bundle_root"]), "--detached"]
    if req.run_id:
        run_args.extend(["--run-id", req.run_id])
    if req.force:
        run_args.append("--force")
    return run_mn_blueprint_run(run_args, cwd=launch["bundle_root"])


def runtime_active_jobs_payload() -> object | None:
    try:
        return json.loads(state.client.list_jobs(0, False))
    except Exception:
        return None


def runtime_active_job_ids(payload: object | None = None) -> set[str] | None:
    if payload is None:
        payload = runtime_active_jobs_payload()
    if payload is None:
        return None
    return active_job_ids_from_jobs_payload(payload)


def runtime_blueprint_web_ui_reserved_ports() -> set[int]:
    active_jobs_payload = runtime_active_jobs_payload()
    active_job_ids = runtime_active_job_ids(active_jobs_payload)
    ports = scheduler_allocated_ports_from_jobs_payload(
        active_jobs_payload,
        active_job_ids=active_job_ids,
    )
    for job_id in active_job_ids or set():
        try:
            job_payload = json.loads(state.client.get_job(job_id))
        except Exception:
            continue
        ports.update(scheduler_allocated_ports_from_jobs_payload(job_payload))
    try:
        payload = json.loads(
            state.client.resolve_service(
                "blueprint-web-ui",
                tags=["web_ui"],
                passing_only=False,
            )
        )
    except Exception:
        return ports
    ports.update(service_ports_from_payload(payload, live_only=True))
    return ports


def service_ports_from_payload(
    payload: object,
    *,
    active_job_ids: set[str] | None = None,
    live_only: bool = False,
) -> set[int]:
    if not isinstance(payload, dict):
        return set()
    services = payload.get("services")
    if not isinstance(services, list):
        return set()
    ports: set[int] = set()
    for service in services:
        if not isinstance(service, dict):
            continue
        if active_job_ids is not None and active_job_ids and str(service.get("job_id") or "") not in active_job_ids:
            continue
        if live_only and not service_may_be_live(service):
            continue
        raw_port = service.get("port")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports


def service_may_be_live(service: dict[str, object]) -> bool:
    status = str(service.get("status") or "").strip().lower()
    health = service.get("health") if isinstance(service.get("health"), dict) else {}
    health_status = str(health.get("status") or "").strip().lower()
    terminal_statuses = {
        "archived",
        "cancelled",
        "critical",
        "failed",
        "offline",
        "stopped",
        "unavailable",
    }
    return status not in terminal_statuses and health_status not in terminal_statuses
