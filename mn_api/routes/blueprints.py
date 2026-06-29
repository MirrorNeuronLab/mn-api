from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from mn_api import state
from mn_api.blueprints import (
    active_job_ids_from_jobs_payload,
    create_blueprint_run_id,
    filter_blueprints_by_category,
    find_blueprint,
    install_blueprint_runtime_models,
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
    validate_blueprint_hardware_requirements,
    validate_blueprint_inputs,
    validate_blueprint_bundle,
    validate_run_id,
    write_blueprint_job_mapping,
)
from mn_api.bundles import load_uploaded_bundle
from mn_api.config import config_value
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
from mn_api.path_utils import resolve_mn_home
from mn_api.schemas import BlueprintLaunchRequest, BlueprintRunRequest


router = APIRouter(prefix="/api/v1")
PROGRESS_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,220}$")
TERMINAL_LAUNCH_PROGRESS_STATUSES = {"completed", "failed"}
CONTEXT_ENGINE_EXPECTATION = (
    "This co-worker uses context memory. First launch may download the context model "
    "and start the Membrane context engine; keep Docker running and be patient."
)


@dataclass(frozen=True)
class LaunchPreflight:
    model_install: dict[str, Any]
    env_overrides: dict[str, str]


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
def install_blueprint(
    blueprint_id: str,
    force: bool = Query(False, description="Force model install when hardware compatibility checks fail."),
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(state.config, blueprint_id)
    validate_blueprint_bundle(repo_root, blueprint)
    model_install = install_blueprint_runtime_models(repo_root, blueprint, force=force)
    if not model_install.get("ok", True):
        raise HTTPException(
            status_code=500,
            detail={
                "error": "blueprint_model_install_failed",
                "models": model_install.get("models") or [],
                "errors": model_install.get("errors") or [],
            },
        )
    return {"installed": True, "blueprint": blueprint, "model_install": model_install}


@router.post("/blueprints/launch/validate")
def validate_blueprint_launch(req: BlueprintLaunchRequest, _auth=Depends(require_auth)):
    launch = resolve_launch_source(req)
    state.close_client()
    validation = run_mn_blueprint_validate(launch["bundle_root"])
    if validation.get("ok"):
        validation = validate_launch_hardware_requirements(launch, force=bool(req.force))
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
    progress_id = validate_progress_id(req.progress_id)
    record_launch_progress(
        progress_id,
        "resolve_source",
        "running",
        "Resolving blueprint source.",
        label="Find blueprint",
        detail="Finding the blueprint files and effective launch configuration.",
    )
    try:
        launch = resolve_launch_source(req)
    except HTTPException:
        record_launch_progress(
            progress_id,
            "resolve_source",
            "failed",
            "Blueprint source could not be resolved.",
            label="Find blueprint",
            detail="The requested blueprint source could not be loaded.",
            severity="error",
        )
        record_launch_progress(progress_id, "launch", "failed", "Blueprint launch stopped before model checks.")
        raise
    record_launch_progress(
        progress_id,
        "resolve_source",
        "completed",
        "Blueprint source resolved.",
        {"source": launch["source"]},
        label="Find blueprint",
        detail="The blueprint source and manifest were found.",
    )
    force = bool(req.force)
    state.close_client()
    config_overrides = dict(req.config_overwrite or req.config_overrides or {})
    preflight = run_launch_preflight(
        launch["repo_root"],
        launch["blueprint"],
        progress_id=progress_id,
        force=force,
        config_overrides=config_overrides,
        source=launch["source"],
    )
    if isinstance(preflight, JSONResponse):
        return preflight
    model_install = preflight.model_install
    env_overrides = dict(preflight.env_overrides)
    validation = {"ok": True, "status": "skipped" if force else "passed", "issues": [], "errors": []}
    if not force:
        record_launch_progress(
            progress_id,
            "validation",
            "running",
            "Validating blueprint.",
            label="Validate inputs",
            detail="Checking blueprint structure and launch inputs before submission.",
        )
        validation = run_mn_blueprint_validate(launch["bundle_root"])
        if not validation.get("ok"):
            record_launch_progress(
                progress_id,
                "validation",
                "failed",
                "Blueprint validation failed.",
                {"validation": validation},
                label="Validate inputs",
                detail="A blueprint or input validation check failed.",
                severity="error",
            )
            record_launch_progress(progress_id, "launch", "failed", "Blueprint launch stopped during validation.")
            return validation_problem_response(
                validation,
                status_code=422,
                error="blueprint_validation_failed",
                title="Blueprint validation failed",
                detail="Fix the highlighted blueprint validation issues and launch again.",
                extra={"blueprint": launch["blueprint"], "source": launch["source"], "progress_id": progress_id},
            )
        record_launch_progress(
            progress_id,
            "validation",
            "completed",
            "Blueprint validation passed.",
            {"validation": validation},
            label="Validate inputs",
            detail="Blueprint structure and launch inputs are valid.",
        )
    else:
        record_launch_progress(
            progress_id,
            "validation",
            "skipped",
            "Validation skipped because force=true.",
            label="Validate inputs",
            detail="Validation was skipped by request.",
        )

    record_launch_progress(
        progress_id,
        "submit",
        "running",
        "Submitting blueprint run.",
        label="Submit runtime job",
        detail="Handing the prepared blueprint to the MirrorNeuron runtime.",
    )
    run_result = run_launch_with_mn_cli(launch, req, env_overrides=env_overrides)
    if not run_result.get("ok"):
        record_launch_progress(
            progress_id,
            "submit",
            "failed",
            str(run_result.get("error") or "mn blueprint run failed"),
            label="Submit runtime job",
            detail="The runtime did not accept the blueprint run.",
            severity="error",
        )
        record_launch_progress(progress_id, "launch", "failed", "Blueprint launch failed during submit.")
        raise HTTPException(status_code=500, detail=run_result.get("error") or "mn blueprint run failed")
    job_id = run_result.get("job_id")
    if not job_id:
        record_launch_progress(
            progress_id,
            "submit",
            "failed",
            "mn blueprint run did not report a Job ID.",
            label="Submit runtime job",
            detail="The runtime accepted output was missing a job id.",
            severity="error",
        )
        record_launch_progress(progress_id, "launch", "failed", "Blueprint launch did not return a job id.")
        raise HTTPException(status_code=500, detail="mn blueprint run did not report a Job ID")
    record_launch_progress(
        progress_id,
        "submit",
        "completed",
        "Blueprint run submitted.",
        {"job_id": job_id, "run_id": run_result.get("run_id")},
        label="Submit runtime job",
        detail="The runtime accepted the job and live monitoring can begin.",
    )
    record_launch_progress(progress_id, "launch", "completed", "Launch complete.", {"job_id": job_id, "run_id": run_result.get("run_id")})
    return {
        "job_id": job_id,
        "id": job_id,
        "run_id": run_result.get("run_id"),
        "status": "pending",
        "source": launch["source"],
        "blueprint": launch["blueprint"],
        "validation": validation,
        "model_install": model_install,
        "progress_id": progress_id,
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


@router.get("/blueprints/launch/progress/{progress_id}")
def get_launch_progress(progress_id: str, _auth=Depends(require_auth)):
    resolved_progress_id = validate_progress_id(progress_id)
    events = read_launch_progress(resolved_progress_id)
    latest = events[-1] if events else None
    completed = any(
        event.get("phase") == "launch"
        and str(event.get("status") or "").lower() in TERMINAL_LAUNCH_PROGRESS_STATUSES
        for event in events
    )
    return {
        "version": 1,
        "progress_id": resolved_progress_id,
        "schema_version": "mn.launch_progress.v1",
        "events": events,
        "phases": summarize_launch_progress_phases(events),
        "latest": latest,
        "completed": completed,
        "status": str(latest.get("status") or "pending") if isinstance(latest, dict) else "pending",
        "current_phase": str(latest.get("phase") or latest.get("step") or "") if isinstance(latest, dict) else None,
    }


def run_blueprint_record(
    repo_root,
    blueprint: dict,
    req: BlueprintRunRequest | None = None,
    *,
    validation: dict | None = None,
):
    blueprint_id = blueprint["id"]
    progress_id = validate_progress_id(req.progress_id if req else None)
    record_launch_progress(
        progress_id,
        "resolve_source",
        "completed",
        "Blueprint source resolved.",
        {"source": "catalog", "blueprint_id": blueprint_id},
        label="Find blueprint",
        detail="The catalog blueprint and launch configuration were found.",
    )
    run_id = req.run_id if req and req.run_id else create_blueprint_run_id(blueprint_id)
    validate_run_id(run_id)
    config_overrides = {}
    if req:
        config_overrides = dict(req.config_overwrite or req.config_overrides or {})
    env_overrides = runtime_blueprint_environment_overrides()
    force = bool(req.force) if req else False
    state.close_client()
    preflight = run_launch_preflight(
        repo_root,
        blueprint,
        progress_id=progress_id,
        force=force,
        config_overrides=config_overrides,
        run_id=run_id,
    )
    if isinstance(preflight, JSONResponse):
        return preflight
    model_install = preflight.model_install
    env_overrides.update(preflight.env_overrides)
    record_launch_progress(
        progress_id,
        "cleanup",
        "running",
        "Cleaning up stale blueprint run resources.",
        label="Clean stale runs",
        detail="Removing stale local helpers from earlier runs of this blueprint.",
    )
    cleanup_stale_blueprint_run_processes(
        repo_root,
        blueprint,
        keep_run_id=run_id,
        active_job_ids=runtime_active_job_ids(),
        reason="stale_blueprint_start",
    )
    record_launch_progress(
        progress_id,
        "cleanup",
        "completed",
        "Stale run cleanup complete.",
        label="Clean stale runs",
        detail="Old local helpers are cleaned up.",
    )
    record_launch_progress(
        progress_id,
        "pre_launch",
        "running",
        "Starting blueprint pre-launch hooks.",
        label="Start local helpers",
        detail="Starting local helper processes needed before the runtime job begins.",
    )
    try:
        start_blueprint_pre_launch_hook(
            repo_root,
            blueprint,
            run_id,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
        )
    except Exception as exc:
        cleanup_blueprint_run_processes(run_id, reason="pre_launch_failed")
        record_launch_progress(
            progress_id,
            "pre_launch",
            "failed",
            str(exc),
            {"run_id": run_id},
            label="Start local helpers",
            detail="A local helper failed before the runtime job could be submitted.",
            severity="error",
        )
        record_launch_progress(progress_id, "launch", "failed", "Blueprint launch failed during pre-launch.", {"run_id": run_id})
        raise
    record_launch_progress(
        progress_id,
        "pre_launch",
        "completed",
        "Pre-launch hooks are ready.",
        label="Start local helpers",
        detail="Local helper processes are ready.",
    )
    if not force:
        record_launch_progress(
            progress_id,
            "validation",
            "running",
            "Validating blueprint runtime and inputs.",
            label="Validate inputs",
            detail="Checking runtime services, models, inputs, and launch config.",
        )
        validation = validate_blueprint_inputs(
            repo_root,
            blueprint,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
        )
        if not validation.get("ok"):
            cleanup_blueprint_run_processes(run_id, reason="validation_failed")
            record_launch_progress(
                progress_id,
                "validation",
                "failed",
                "Blueprint validation failed.",
                {"validation": validation},
                label="Validate inputs",
                detail="A runtime, model, input, or launch config check failed.",
                severity="error",
            )
            record_launch_progress(progress_id, "launch", "failed", "Blueprint launch stopped during validation.", {"run_id": run_id})
            return validation_problem_response(
                validation,
                status_code=422,
                error="blueprint_validation_failed",
                title="Blueprint validation failed",
                detail="Fix the highlighted blueprint runtime or input issue, or pass force=true to run anyway.",
                extra={"run_id": run_id, "blueprint": blueprint, "progress_id": progress_id},
            )
        record_launch_progress(
            progress_id,
            "validation",
            "completed",
            "Blueprint validation passed.",
            {"validation": validation},
            label="Validate inputs",
            detail="Runtime services, models, inputs, and launch config are ready.",
        )
    else:
        record_launch_progress(
            progress_id,
            "validation",
            "skipped",
            "Validation skipped because force=true.",
            label="Validate inputs",
            detail="Validation was skipped by request.",
        )
    try:
        record_launch_progress(
            progress_id,
            "prepare_bundle",
            "running",
            "Preparing the job bundle.",
            label="Package workflow",
            detail="Packaging workflow files, local inputs, and runtime support code.",
        )
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
        record_launch_progress(
            progress_id,
            "prepare_bundle",
            "failed",
            "Job bundle preparation failed.",
            {"run_id": run_id},
            label="Package workflow",
            detail="The job bundle could not be prepared for submission.",
            severity="error",
        )
        record_launch_progress(progress_id, "launch", "failed", "Blueprint launch failed while preparing the bundle.", {"run_id": run_id})
        raise
    record_launch_progress(
        progress_id,
        "prepare_bundle",
        "completed",
        "Job bundle prepared.",
        {"run_id": run_id},
        label="Package workflow",
        detail="Workflow files, inputs, and support code are ready.",
    )

    try:
        record_launch_progress(
            progress_id,
            "submit",
            "running",
            "Submitting job to the runtime.",
            {"run_id": run_id},
            label="Submit runtime job",
            detail="Handing the prepared job bundle to MirrorNeuron core.",
        )
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
        record_launch_progress(
            progress_id,
            "submit",
            "completed",
            "Job submitted to the runtime.",
            {"run_id": run_id, "job_id": job_id},
            label="Submit runtime job",
            detail="The runtime accepted the job and live monitoring can begin.",
        )
        record_launch_progress(progress_id, "launch", "completed", "Launch complete.", {"run_id": run_id, "job_id": job_id})
        return {
            "job_id": job_id,
            "id": job_id,
            "run_id": run_id,
            "status": "pending",
            "blueprint": blueprint,
            "validation": validation,
            "model_install": model_install,
            "progress_id": progress_id,
            "web_ui_service": web_ui_service or None,
        }
    except Exception as exc:
        cleanup_blueprint_run_processes(run_id, reason="launch_failed")
        record_launch_progress(
            progress_id,
            "submit",
            "failed",
            str(exc),
            {"run_id": run_id},
            label="Submit runtime job",
            detail="The runtime did not accept the job bundle.",
            severity="error",
        )
        record_launch_progress(progress_id, "launch", "failed", "Blueprint launch failed during submit.", {"run_id": run_id})
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
        workflow = manifest.get("workflow") if isinstance(manifest.get("workflow"), dict) else {}
        workflow_manifest = manifest.get("apiVersion") == "mn.workflow/v1" or manifest.get("kind") == "Workflow" or isinstance(manifest.get("workflow"), dict)
        blueprint_id = sanitize_blueprint_id(
            manifest.get("id")
            or manifest.get("blueprint_id")
            or manifest.get("workflow_id")
            or workflow.get("workflow_id")
            or (None if workflow_manifest else manifest.get("graph_id"))
            or bundle_root.name,
            "uploaded_bundle",
        )
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


def validate_progress_id(progress_id: str | None) -> str | None:
    if progress_id is None:
        return None
    value = str(progress_id).strip()
    if not value:
        return None
    if not PROGRESS_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="invalid progress id")
    return value


def launch_progress_root() -> Path:
    configured = config_value("MN_LAUNCH_PROGRESS_DIR")
    if configured:
        return Path(configured).expanduser()
    return resolve_mn_home() / "launch_progress"


def launch_progress_path(progress_id: str) -> Path:
    return launch_progress_root() / f"{progress_id}.jsonl"


def record_launch_progress(
    progress_id: str | None,
    phase: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    detail: str | None = None,
    expectation: str | None = None,
    severity: str | None = None,
) -> None:
    if not progress_id:
        return
    event: dict[str, Any] = {
        "version": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": phase,
        "status": status,
        "message": message,
    }
    if label:
        event["label"] = label
    if detail:
        event["detail"] = detail
    if expectation:
        event["expectation"] = expectation
    if severity:
        event["severity"] = severity
    if details is not None:
        event["details"] = details
    try:
        path = launch_progress_path(progress_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str, separators=(",", ":")) + "\n")
    except OSError:
        state.logger.exception("Failed to record blueprint launch progress")


def read_launch_progress(progress_id: str | None) -> list[dict[str, Any]]:
    if not progress_id:
        return []
    path = launch_progress_path(progress_id)
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        state.logger.exception("Failed to read blueprint launch progress")
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def launch_progress_phase_label(phase: str) -> str:
    labels = {
        "resolve_source": "Find blueprint",
        "requirements": "Check runtime resources",
        "model_install": "Prepare runtime models",
        "context_engine": "Prepare context memory",
        "validation": "Validate inputs",
        "cleanup": "Clean stale runs",
        "pre_launch": "Start local helpers",
        "prepare_bundle": "Package workflow",
        "submit": "Submit runtime job",
        "launch": "Launch",
    }
    return labels.get(phase, phase.replace("_", " ").title())


def summarize_launch_progress_phases(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
    for event in events:
        phase = str(event.get("phase") or event.get("step") or event.get("id") or "").strip()
        if not phase:
            continue
        phase_record = {
            "version": int(event.get("version") or 1),
            "id": phase,
            "label": str(event.get("label") or launch_progress_phase_label(phase)),
            "status": str(event.get("status") or event.get("state") or "running"),
            "message": str(event.get("message") or event.get("detail") or ""),
            "updated_at": str(event.get("ts") or event.get("timestamp") or ""),
        }
        for key in ("detail", "expectation", "severity", "details"):
            if key in event:
                phase_record[key] = event[key]
        phases[phase] = phase_record
    return list(phases.values())


def model_install_progress_message(model_install: dict[str, Any]) -> str:
    models = model_install.get("models") or []
    services = model_install.get("services") or []
    if not models and not services:
        return "No runtime model dependencies declared."
    counts: dict[str, int] = {}
    for item in models:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    parts = []
    labels = {
        "installed": "installed",
        "already_installed": "already present",
        "service_required": "service model noted",
        "service_registry": "service endpoint selected",
        "model_remote": "remote endpoint selected",
        "explicit_config": "configured endpoint selected",
        "cluster_provided": "cluster endpoint selected",
        "failed": "failed",
    }
    for status in (
        "installed",
        "already_installed",
        "service_required",
        "service_registry",
        "model_remote",
        "explicit_config",
        "cluster_provided",
        "failed",
    ):
        count = counts.get(status, 0)
        if count:
            noun = labels[status]
            parts.append(f"{count} {noun}")
    remaining = sum(count for status, count in counts.items() if status not in labels)
    if remaining:
        parts.append(f"{remaining} checked")
    service_count = len(services)
    if service_count:
        parts.append(f"{service_count} service{'s' if service_count != 1 else ''} ready")
    return "Runtime models ready: " + ", ".join(parts) + "."


def run_launch_preflight(
    repo_root: Path,
    blueprint: dict[str, Any],
    *,
    progress_id: str | None,
    force: bool,
    config_overrides: dict[str, Any],
    source: str | None = None,
    run_id: str | None = None,
) -> LaunchPreflight | JSONResponse:
    run_details = {"run_id": run_id} if run_id else None
    context_phase_seen = False

    def record_service_progress(event: str, result: dict[str, Any] | None) -> None:
        nonlocal context_phase_seen
        if event == "context_engine_needed":
            context_phase_seen = True
            record_launch_progress(
                progress_id,
                "context_engine",
                "running",
                "Preparing context memory for this blueprint.",
                {**(run_details or {}), "service": "membrane-context-engine"},
                label="Prepare context memory",
                detail="Starting the Membrane context engine and ensuring its model is available.",
                expectation=CONTEXT_ENGINE_EXPECTATION,
            )
            return
        if event == "context_engine_ready":
            context_phase_seen = True
            details = {**(run_details or {}), "service": "membrane-context-engine"}
            if result:
                details["context_engine"] = result
            record_launch_progress(
                progress_id,
                "context_engine",
                "completed",
                "Context memory service is ready.",
                details,
                label="Prepare context memory",
                detail="The Membrane context engine is running and ready for the blueprint.",
                expectation=CONTEXT_ENGINE_EXPECTATION,
            )
            return
        if event == "context_engine_failed":
            context_phase_seen = True
            details = {**(run_details or {}), "service": "membrane-context-engine"}
            if result:
                details["context_engine"] = result
            record_launch_progress(
                progress_id,
                "context_engine",
                "failed",
                str((result or {}).get("error") or "Context memory service could not be prepared."),
                details,
                label="Prepare context memory",
                detail="The Membrane context engine did not become ready.",
                expectation=CONTEXT_ENGINE_EXPECTATION,
                severity="error",
            )

    record_launch_progress(
        progress_id,
        "requirements",
        "running",
        "Checking runtime hardware requirements.",
        run_details,
        label="Check runtime resources",
        detail="Confirming this runtime has the resources requested by the blueprint.",
    )
    requirements_validation = validate_blueprint_hardware_requirements(repo_root, blueprint, force=force)
    if not requirements_validation.get("ok"):
        failed_details = {"validation": requirements_validation}
        if run_id:
            failed_details["run_id"] = run_id
        record_launch_progress(
            progress_id,
            "requirements",
            "failed",
            "Runtime hardware requirements are not available.",
            failed_details,
            label="Check runtime resources",
            detail="The runtime does not currently satisfy this blueprint's hardware requirements.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch needs a matching runtime node.",
            run_details,
        )
        return requirements_problem_response(
            requirements_validation,
            blueprint=blueprint,
            source=source,
            run_id=run_id,
            progress_id=progress_id,
        )
    completed_details = {"validation": requirements_validation}
    if run_id:
        completed_details["run_id"] = run_id
    record_launch_progress(
        progress_id,
        "requirements",
        "completed",
        "Runtime hardware requirements satisfied.",
        completed_details,
        label="Check runtime resources",
        detail="The runtime has the resources needed to start this blueprint.",
    )
    record_launch_progress(
        progress_id,
        "model_install",
        "running",
        "Checking required runtime models.",
        label="Prepare runtime models",
        detail="Some first launches may download Docker Model Runner models before the job can start.",
        expectation="Model downloads can take several minutes on a first launch. Keep Docker running.",
    )
    try:
        model_install = install_blueprint_runtime_models(
            repo_root,
            blueprint,
            force=force,
            config_overrides=config_overrides,
            service_progress=record_service_progress,
        )
    except HTTPException:
        record_launch_progress(
            progress_id,
            "model_install",
            "failed",
            "Runtime model install failed.",
            label="Prepare runtime models",
            detail="A required runtime model could not be prepared.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch failed before validation.",
            run_details,
        )
        raise
    except Exception as exc:
        record_launch_progress(
            progress_id,
            "model_install",
            "failed",
            str(exc),
            label="Prepare runtime models",
            detail="A required runtime model could not be prepared.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch failed before validation.",
            run_details,
        )
        raise
    if not model_install.get("ok", True):
        record_launch_progress(
            progress_id,
            "model_install",
            "failed",
            "Runtime model install failed.",
            {"model_install": model_install},
            label="Prepare runtime models",
            detail="A required runtime model could not be prepared.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch failed before validation.",
            run_details,
        )
        return model_install_problem_response(
            model_install,
            blueprint=blueprint,
            source=source,
            run_id=run_id,
            progress_id=progress_id,
        )
    record_launch_progress(
        progress_id,
        "model_install",
        "completed",
        model_install_progress_message(model_install),
        {"model_install": model_install},
        label="Prepare runtime models",
        detail="Required runtime models are ready.",
    )
    if not context_phase_seen:
        record_launch_progress(
            progress_id,
            "context_engine",
            "skipped",
            "Context memory service is not required for this blueprint.",
            run_details,
            label="Prepare context memory",
            detail="This blueprint does not request context memory.",
        )
    env_patch = model_install.get("env") if isinstance(model_install.get("env"), dict) else {}
    env_overrides = {str(key): str(value) for key, value in env_patch.items() if value is not None}
    return LaunchPreflight(model_install=model_install, env_overrides=env_overrides)


def validate_launch_hardware_requirements(launch: dict, *, force: bool = False) -> dict[str, Any]:
    return validate_blueprint_hardware_requirements(
        launch["repo_root"],
        launch["blueprint"],
        force=force,
    )


def requirements_problem_response(
    validation: dict,
    *,
    blueprint: dict,
    source: str | None = None,
    run_id: str | None = None,
    progress_id: str | None = None,
) -> JSONResponse:
    extra: dict[str, Any] = {"blueprint": blueprint}
    if source:
        extra["source"] = source
    if run_id:
        extra["run_id"] = run_id
    if progress_id:
        extra["progress_id"] = progress_id
    return validation_problem_response(
        validation,
        status_code=412,
        error="requirements_not_met",
        title="Runtime node required",
        detail="Add or connect a runtime node that meets this blueprint's hardware requirements, then launch again.",
        extra=extra,
    )


def model_install_problem_response(
    model_install: dict,
    *,
    blueprint: dict,
    source: str | None = None,
    run_id: str | None = None,
    progress_id: str | None = None,
) -> JSONResponse:
    issues = []
    for item in model_install.get("models") or []:
        if str(item.get("status") or "") != "failed":
            continue
        message = str(item.get("error") or f"Could not install {item.get('model') or item.get('id') or 'runtime model'}")
        issues.append(
            {
                "code": "runtime_model_install_failed",
                "message": message,
                "help": "Check hardware compatibility or retry with force=true if you intentionally want to bypass model policy.",
                "severity": "error",
                "location": {"path": str(item.get("path") or "runtime.models")},
            }
        )
    if not issues:
        issues = [
            {
                "code": "runtime_model_install_failed",
                "message": str(error),
                "help": "Check hardware compatibility or retry with force=true if you intentionally want to bypass model policy.",
                "severity": "error",
            }
            for error in model_install.get("errors") or ["Required runtime model could not be installed."]
        ]
    validation = {
        "version": 1,
        "schema_version": "validation.report/v1",
        "ok": False,
        "status": "failed",
        "error_count": len(issues),
        "errors": [str(issue.get("message") or "") for issue in issues],
        "issues": issues,
        "results": model_install.get("models") or [],
    }
    extra = {
        "blueprint": blueprint,
        "model_install": model_install,
    }
    if source:
        extra["source"] = source
    if run_id:
        extra["run_id"] = run_id
    if progress_id:
        extra["progress_id"] = progress_id
    return validation_problem_response(
        validation,
        status_code=422,
        error="blueprint_model_install_failed",
        title="Blueprint model install failed",
        detail="A required runtime model could not be installed automatically.",
        extra=extra,
    )


def run_launch_with_mn_cli(
    launch: dict,
    req: BlueprintLaunchRequest,
    *,
    env_overrides: dict[str, str] | None = None,
) -> dict:
    run_args = ["--folder", str(launch["bundle_root"]), "--detached"]
    if req.run_id:
        run_args.extend(["--run-id", req.run_id])
    if req.force:
        run_args.append("--force")
    return run_mn_blueprint_run(run_args, cwd=launch["bundle_root"], env_overrides=env_overrides)


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
