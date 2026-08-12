from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from mn_sdk import (
    cleanup_job_definition_resources,
    cleanup_blueprint_resources,
    generate_job_definition_submission_id,
    generate_stable_job_id,
    load_blueprint_index,
    load_model_ownership,
    remove_model_owner,
    remove_model_record,
    remove_model_ref,
)
from mn_sdk.blueprint_source import run_git

from mn_api import state
from mn_api.blueprints import (
    active_job_ids_from_jobs_payload,
    create_blueprint_run_id,
    expand_blueprint_manifest_if_source,
    filter_blueprints_by_category,
    find_blueprint,
    install_blueprint_runtime_models,
    load_blueprint_categories,
    load_blueprint_bundle,
    load_blueprint_catalog,
    cleanup_blueprint_run_processes,
    cleanup_stale_blueprint_run_processes,
    deep_merge,
    defer_blueprint_runtime_models,
    runtime_blueprint_environment_overrides,
    start_background_event_relay_if_needed,
    start_blueprint_pre_launch_hook,
    local_blueprint_from_path,
    run_mn_blueprint_validate,
    sanitize_blueprint_id,
    validate_blueprint_hardware_requirements,
    validate_blueprint_inputs,
    validate_blueprint_bundle,
    validate_run_id,
    write_blueprint_job_mapping,
)
from mn_api.bundles import load_uploaded_bundle
from mn_api.blueprint_secret_environment import (
    inject_declared_secret_environment,
    manifest_without_secret_environment,
    requested_secret_environment,
    validate_blueprint_secret_environment,
)
from mn_api.config import config_value
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error, validation_problem_response
from mn_api.path_utils import resolve_mn_home
from mn_api.schemas import BlueprintCleanupRequest, BlueprintLaunchRequest, BlueprintRunRequest, BlueprintUninstallRequest, BlueprintUpdateRequest


router = APIRouter(prefix="/api/v2")
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
    config_overrides: dict[str, Any]


def _current_config():
    return state.refresh_config_from_env()


@router.get("/blueprints")
def list_blueprints(
    category: str | None = Query(
        default=None,
        description="Optional blueprint category name or slug. Comma-separated values are allowed.",
    ),
    _auth=Depends(require_auth),
):
    repo_root, blueprints = load_blueprint_catalog(_current_config())
    categories = load_blueprint_categories(repo_root, blueprints)
    filtered_blueprints = filter_blueprints_by_category(blueprints, category)
    return {"repo_dir": str(repo_root), "blueprints": filtered_blueprints, "categories": categories}


@router.get("/blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, _auth=Depends(require_auth)):
    _repo_root, blueprint = find_blueprint(_current_config(), blueprint_id)
    return {"blueprint": blueprint}


@router.post("/blueprints/{blueprint_id}/install")
def install_blueprint(
    blueprint_id: str,
    force: bool = Query(False, description="Force model install when hardware compatibility checks fail."),
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(_current_config(), blueprint_id)
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
    repo_root, blueprint = find_blueprint(_current_config(), blueprint_id)
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
    resolved_req = resolve_async_blueprint_launch_request(req)
    record_launch_progress(
        resolved_req.progress_id,
        "launch",
        "running",
        "Blueprint launch accepted.",
        {"run_id": resolved_req.run_id, "source": resolved_req.source},
        label="Launch",
        detail="The blueprint launch is running in the background.",
    )
    start_async_blueprint_launch(resolved_req)
    return JSONResponse(
        status_code=202,
        content={
            "version": 2,
            "status": "launching",
            "run_id": resolved_req.run_id,
            "job_id": None,
            "source": resolved_req.source,
            "progress_id": resolved_req.progress_id,
            "progress_url": f"/api/v2/blueprints/launch/progress/{resolved_req.progress_id}",
        },
    )


def run_blueprint_launch_record(req: BlueprintLaunchRequest):
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
    run_req = BlueprintRunRequest(
        version=req.version,
        job_id=req.job_id,
        run_id=req.run_id,
        config_overwrite=req.config_overwrite,
        config_overrides=req.config_overrides,
        secret_environment=req.secret_environment,
        force=req.force,
        progress_id=progress_id,
        fake_llm=req.fake_llm,
        fake_skills=req.fake_skills,
    )
    return run_blueprint_record(
        launch["repo_root"],
        launch["blueprint"],
        run_req,
        source=launch["source"],
    )


@router.post("/blueprints/{blueprint_id}/runs")
def run_blueprint(
    blueprint_id: str,
    req: BlueprintRunRequest | None = None,
    _auth=Depends(require_auth),
):
    repo_root, blueprint = find_blueprint(_current_config(), blueprint_id)
    resolved_req = resolve_async_blueprint_run_request(blueprint_id, req)
    record_launch_progress(
        resolved_req.progress_id,
        "launch",
        "running",
        "Blueprint launch accepted.",
        {"run_id": resolved_req.run_id, "blueprint_id": blueprint_id},
        label="Launch",
        detail="The blueprint launch is being submitted to the runtime.",
    )
    result = run_blueprint_record(repo_root, blueprint, resolved_req)
    execution_id = str(result.get("run_id") or result.get("id") or "")
    return {
        **result,
        "version": 2,
        "execution_id": execution_id or None,
    }


@router.get("/blueprints/launch/progress/{progress_id}")
def get_launch_progress(progress_id: str, _auth=Depends(require_auth)):
    return launch_progress_snapshot(progress_id)


def launch_progress_snapshot(progress_id: str) -> dict[str, Any]:
    resolved_progress_id = validate_progress_id(progress_id)
    events = read_launch_progress(resolved_progress_id)
    latest = events[-1] if events else None
    terminal = latest_terminal_launch_event(events)
    ids = launch_progress_identifiers(events)
    error = launch_progress_error(events)
    completed = any(
        event.get("phase") == "launch"
        and str(event.get("status") or "").lower() in TERMINAL_LAUNCH_PROGRESS_STATUSES
        for event in events
    )
    response = {
        "version": 2,
        "progress_id": resolved_progress_id,
        "schema_version": "mn.launch_progress.v2",
        "run_id": ids.get("run_id"),
        "job_id": ids.get("job_id"),
        "events": events,
        "phases": summarize_launch_progress_phases(events),
        "latest": latest,
        "completed": completed,
        "status": (
            str(terminal.get("status") or "pending")
            if isinstance(terminal, dict)
            else str(latest.get("status") or "pending")
            if isinstance(latest, dict)
            else "pending"
        ),
        "current_phase": str(latest.get("phase") or latest.get("step") or "") if isinstance(latest, dict) else None,
    }
    if error:
        response["error"] = error
    return response


def resolve_async_blueprint_run_request(blueprint_id: str, req: BlueprintRunRequest | None) -> BlueprintRunRequest:
    if req is None:
        req = BlueprintRunRequest()
    run_id = req.run_id or create_blueprint_run_id(blueprint_id)
    validate_run_id(run_id)
    progress_id = validate_progress_id(req.progress_id) or create_blueprint_progress_id(run_id)
    return BlueprintRunRequest(
        version=req.version,
        job_id=req.job_id,
        run_id=run_id,
        config_overwrite=req.config_overwrite,
        config_overrides=req.config_overrides,
        secret_environment=req.secret_environment,
        force=req.force,
        progress_id=progress_id,
        fake_llm=req.fake_llm,
        fake_skills=req.fake_skills,
    )


def resolve_async_blueprint_launch_request(req: BlueprintLaunchRequest) -> BlueprintLaunchRequest:
    run_id = req.run_id or create_blueprint_run_id(blueprint_launch_run_id_seed(req))
    validate_run_id(run_id)
    progress_id = validate_progress_id(req.progress_id) or create_blueprint_progress_id(run_id)
    return BlueprintLaunchRequest(
        version=req.version,
        job_id=req.job_id,
        run_id=run_id,
        config_overwrite=req.config_overwrite,
        config_overrides=req.config_overrides,
        secret_environment=req.secret_environment,
        force=req.force,
        progress_id=progress_id,
        fake_llm=req.fake_llm,
        fake_skills=req.fake_skills,
        source=req.source,
        blueprint_id=req.blueprint_id,
        path=req.path,
        **{"_bundle_path": req.bundle_path},
    )


def blueprint_launch_run_id_seed(req: BlueprintLaunchRequest) -> str:
    if req.blueprint_id:
        return sanitize_blueprint_id(req.blueprint_id)
    for value in (req.path, req.bundle_path):
        if value:
            return sanitize_blueprint_id(Path(value).expanduser().name or Path(value).stem)
    return sanitize_blueprint_id(req.source or "blueprint")


def fake_mode_environment_overrides(req: BlueprintRunRequest | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if req and req.fake_llm:
        env.update(
            {
                "MN_BLUEPRINT_FAKE_LLM": "1",
                "MN_BLUEPRINT_LLM_MODE": "fake",
                "MN_LLM_PROVIDER": "fake",
                "MN_LLM_MODEL": "fake-deterministic-blueprint-agent",
            }
        )
    if req and req.fake_skills:
        env["MN_BLUEPRINT_FAKE_SKILLS"] = "1"
    return env


def create_blueprint_progress_id(run_id: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(run_id or "blueprint-run")).strip("_.:-") or "blueprint-run"
    suffix = hashlib.sha1(f"{base}:{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
    progress_id = f"{base}-{stamp}-{suffix}"
    if len(progress_id) <= 220:
        return progress_id
    keep = 220 - len(stamp) - len(suffix) - 2
    return f"{base[:keep].rstrip('_.:-')}-{stamp}-{suffix}"


def start_async_blueprint_run(repo_root: Path, blueprint: dict[str, Any], req: BlueprintRunRequest) -> threading.Thread:
    thread = threading.Thread(
        target=run_blueprint_record_background,
        args=(repo_root, blueprint, req),
        name=f"mn-blueprint-run-{req.run_id or blueprint.get('id') or 'unknown'}",
        daemon=True,
    )
    thread.start()
    return thread


def start_async_blueprint_launch(req: BlueprintLaunchRequest) -> threading.Thread:
    thread = threading.Thread(
        target=run_blueprint_launch_background,
        args=(req,),
        name=f"mn-blueprint-launch-{req.run_id or req.source or 'unknown'}",
        daemon=True,
    )
    thread.start()
    return thread


def run_blueprint_launch_background(req: BlueprintLaunchRequest) -> None:
    progress_id = req.progress_id
    run_id = req.run_id
    try:
        result = run_blueprint_launch_record(req)
        if isinstance(result, JSONResponse) and not launch_progress_has_terminal_event(progress_id):
            record_launch_progress(
                progress_id,
                "launch",
                "failed",
                json_response_error_message(result),
                {"run_id": run_id, "status_code": result.status_code},
                label="Launch",
                detail="Blueprint launch failed.",
                severity="error",
            )
    except Exception as exc:
        state.logger.exception("Async blueprint launch failed")
        if not launch_progress_has_terminal_event(progress_id):
            record_launch_progress(
                progress_id,
                "launch",
                "failed",
                str(exc) or "Blueprint launch failed.",
                {"run_id": run_id},
                label="Launch",
                detail="Blueprint launch failed.",
                severity="error",
            )


def run_blueprint_record_background(repo_root: Path, blueprint: dict[str, Any], req: BlueprintRunRequest) -> None:
    progress_id = req.progress_id
    run_id = req.run_id
    try:
        result = run_blueprint_record(repo_root, blueprint, req)
        if isinstance(result, JSONResponse) and not launch_progress_has_terminal_event(progress_id):
            record_launch_progress(
                progress_id,
                "launch",
                "failed",
                json_response_error_message(result),
                {"run_id": run_id, "status_code": result.status_code},
                label="Launch",
                detail="Blueprint launch failed.",
                severity="error",
            )
    except Exception as exc:
        state.logger.exception("Async blueprint run failed")
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            str(exc) or "Blueprint launch failed.",
            {"run_id": run_id},
            label="Launch",
            detail="Blueprint launch failed.",
            severity="error",
        )


def json_response_error_message(response: JSONResponse) -> str:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except Exception:
        return "Blueprint launch failed."
    if isinstance(payload, dict):
        for key in ("detail", "title", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and str(first.get("message") or "").strip():
                return str(first["message"]).strip()
            if isinstance(first, str) and first.strip():
                return first.strip()
    return "Blueprint launch failed."


def launch_progress_has_terminal_event(progress_id: str | None) -> bool:
    return latest_terminal_launch_event(read_launch_progress(progress_id)) is not None


def latest_terminal_launch_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("phase") != "launch":
            continue
        if str(event.get("status") or "").lower() in TERMINAL_LAUNCH_PROGRESS_STATUSES:
            return event
    return None


def launch_progress_identifiers(events: list[dict[str, Any]]) -> dict[str, str | None]:
    ids: dict[str, str | None] = {"run_id": None, "job_id": None}
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        for key in ("run_id", "job_id"):
            value = details.get(key)
            if value is not None:
                ids[key] = str(value)
    return ids


def launch_progress_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if str(event.get("status") or "").lower() != "failed":
            continue
        message = str(event.get("message") or "").strip()
        if message:
            return message
        detail = str(event.get("detail") or "").strip()
        if detail:
            return detail
    return None


@router.post("/blueprints:cleanup")
def cleanup_blueprints(req: BlueprintCleanupRequest, _auth=Depends(require_auth)):
    explicit_ids = {req.blueprint_id} if req.blueprint_id else set()
    active_ids = set()
    stale_processes = False

    if req.blueprint_id and not req.include_files and not req.include_docker:
        repo_root, blueprint = find_blueprint(_current_config(), req.blueprint_id)
        if not req.dry_run:
            cleanup_stale_blueprint_run_processes(
                repo_root,
                blueprint,
                active_job_ids=runtime_active_job_ids(),
                reason="api_blueprint_cleanup",
            )
        stale_processes = True
    elif req.include_dead and not explicit_ids:
        active_ids = blueprint_ids_from_storage(resolve_blueprint_storage(req.source))

    summary = cleanup_blueprint_resources(
        blueprint_ids=explicit_ids,
        active_blueprint_ids=active_ids,
        python_envs_dir=optional_path(req.python_envs_dir),
        runs_root=optional_path(req.runs_root),
        generated_bundles_dir=optional_path(req.generated_bundles_dir),
        bundle_cache_dir=optional_path(req.bundle_cache_dir),
        include_dead=req.include_dead,
        include_docker=req.include_docker,
        include_files=req.include_files,
        dry_run=req.dry_run,
    )
    return {
        "status": "planned" if req.dry_run else "completed",
        "blueprint_id": req.blueprint_id,
        "active_blueprint_ids": sorted(active_ids),
        "stale_processes": stale_processes,
        "summary": summary,
    }


@router.post("/blueprints:update")
def update_blueprints(req: BlueprintUpdateRequest, _auth=Depends(require_auth)):
    storage_dir = resolve_blueprint_storage(req.source)
    if not storage_dir.exists():
        raise HTTPException(status_code=404, detail=f"Blueprint storage not found at {storage_dir}")
    before_ids = blueprint_ids_from_storage(storage_dir)
    warning = None
    try:
        git_result = run_git(["-C", str(storage_dir), "pull", "--ff-only"])
    except subprocess.CalledProcessError as exc:
        warning = (exc.stderr or exc.stdout or str(exc)).strip()
        git_result = None

    try:
        load_blueprint_index(storage_dir / "index.json")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error loading updated blueprint index: {exc}") from exc

    after_ids = blueprint_ids_from_storage(storage_dir)
    removed_ids = before_ids - after_ids
    cleanup_summary = None
    if removed_ids or after_ids:
        cleanup_summary = cleanup_blueprint_resources(
            blueprint_ids=removed_ids,
            active_blueprint_ids=after_ids,
            include_dead=True,
            include_docker=True,
            dry_run=False,
        )
    return {
        "status": "completed",
        "storage": str(storage_dir),
        "blueprints_before": len(before_ids),
        "blueprints_after": len(after_ids),
        "blueprints_removed": sorted(removed_ids),
        "git": {
            "stdout": (git_result.stdout or "").strip() if git_result else "",
            "stderr": (git_result.stderr or "").strip() if git_result else warning or "",
            "warning": warning,
        },
        "cleanup": cleanup_summary,
    }


@router.post("/blueprints:uninstall")
def uninstall_blueprints(req: BlueprintUninstallRequest, _auth=Depends(require_auth)):
    if req.keep_models and req.remove_models:
        raise HTTPException(status_code=400, detail="Use only one of keep_models or remove_models.")

    storage_dir = resolve_blueprint_storage(req.source)
    blueprint_ids = {req.blueprint_id} if req.blueprint_id else blueprint_ids_from_storage(storage_dir)
    archive_path = None
    storage_removed = False
    if req.blueprint_id:
        archive_path = archive_blueprint_install(req.blueprint_id, storage_dir=storage_dir, dry_run=req.dry_run)
    elif storage_dir.exists() and not req.dry_run:
        shutil.rmtree(storage_dir)
        storage_removed = True

    cleanup_summary = None
    if not req.keep_resources:
        cleanup_summary = cleanup_blueprint_resources(
            blueprint_ids=blueprint_ids,
            active_blueprint_ids=set(),
            include_dead=True,
            include_docker=True,
            include_files=True,
            dry_run=req.dry_run,
        )

    model_summary = uninstall_blueprint_models(
        req.blueprint_id,
        keep_models=req.keep_models,
        remove_models=req.remove_models,
        dry_run=req.dry_run,
    )
    return {
        "status": "planned" if req.dry_run else "completed",
        "blueprint_id": req.blueprint_id,
        "storage": str(storage_dir),
        "storage_removed": storage_removed,
        "archive": str(archive_path) if archive_path else None,
        "blueprint_ids": sorted(blueprint_ids),
        "cleanup": cleanup_summary,
        "models": model_summary,
    }


def optional_path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def resolve_blueprint_storage(source: str | None) -> Path:
    if source:
        return Path(source).expanduser()
    repo_root, _blueprints = load_blueprint_catalog(_current_config())
    return repo_root


def blueprint_ids_from_storage(storage_dir: Path) -> set[str]:
    index_path = storage_dir / "index.json"
    if not index_path.exists():
        return set()
    try:
        entries = load_blueprint_index(index_path)
    except Exception:
        return set()
    ids: set[str] = set()
    for entry in entries:
        blueprint_id = entry.get("id")
        if isinstance(blueprint_id, str) and blueprint_id.strip():
            ids.add(blueprint_id.strip())
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.strip():
            manifest_path = storage_dir / path / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            metadata = manifest.get("metadata") if isinstance(manifest, dict) else {}
            manifest_blueprint_id = metadata.get("blueprint_id") if isinstance(metadata, dict) else None
            if isinstance(manifest_blueprint_id, str) and manifest_blueprint_id.strip():
                ids.add(manifest_blueprint_id.strip())
    return ids


def archive_blueprint_install(blueprint_id: str, *, storage_dir: Path, dry_run: bool) -> Path:
    install_dir = resolve_mn_home() / "blueprint_installs"
    record_path = install_dir / f"{blueprint_id}.json"
    payload: dict[str, Any]
    if record_path.is_file():
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    else:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("version", 2)
    payload.setdefault("schema_version", "mn.blueprint.install.v1")
    payload.setdefault("blueprint_id", blueprint_id)
    payload.setdefault("storage_dir", str(storage_dir))
    payload["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    archive_dir = install_dir / "archive"
    archive_path = archive_dir / f"{blueprint_id}-{int(time.time())}.json"
    if dry_run:
        return archive_path
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record_path.unlink(missing_ok=True)
    return archive_path


def uninstall_blueprint_models(
    blueprint_id: str | None,
    *,
    keep_models: bool,
    remove_models: bool,
    dry_run: bool,
) -> dict[str, Any]:
    if not blueprint_id:
        return {"orphaned": [], "removed": [], "kept": []}
    orphaned = projected_orphaned_models(blueprint_id) if dry_run else remove_model_owner(blueprint_id)
    removed: list[str] = []
    kept: list[str] = []
    if keep_models:
        return {"orphaned": orphaned, "removed": removed, "kept": [model_name_from_record(record) for record in orphaned]}
    for record in orphaned:
        model = model_name_from_record(record)
        if not model:
            continue
        if remove_models:
            if not dry_run:
                remove_model_ref(model, force=True)
                remove_model_record(model)
            removed.append(model)
        else:
            kept.append(model)
    return {"orphaned": orphaned, "removed": removed, "kept": kept}


def projected_orphaned_models(blueprint_id: str) -> list[dict[str, Any]]:
    ledger = load_model_ownership()
    orphaned: list[dict[str, Any]] = []
    for record in ledger.get("models", {}).values():
        if not isinstance(record, dict):
            continue
        owners = dict(record.get("owners") or {})
        owners.pop(blueprint_id, None)
        if owners or record.get("manual") or str(record.get("provider") or "docker_model_runner") != "docker_model_runner":
            continue
        projected = dict(record)
        projected["owners"] = {}
        orphaned.append(projected)
    return orphaned


def model_name_from_record(record: dict[str, Any]) -> str:
    return str(record.get("docker_model") or record.get("model") or "")


def run_blueprint_record(
    repo_root,
    blueprint: dict,
    req: BlueprintRunRequest | None = None,
    *,
    validation: dict | None = None,
    source: str = "catalog",
):
    definition_committed = False
    blueprint_id = blueprint["id"]
    progress_id = validate_progress_id(req.progress_id if req else None)
    record_launch_progress(
        progress_id,
        "resolve_source",
        "completed",
        "Blueprint source resolved.",
        {"source": source, "blueprint_id": blueprint_id},
        label="Find blueprint",
        detail="The catalog blueprint and launch configuration were found.",
    )
    run_id = req.run_id if req and req.run_id else create_blueprint_run_id(blueprint_id)
    validate_run_id(run_id)
    config_overrides = {}
    if req:
        config_overrides = dict(req.config_overwrite or req.config_overrides or {})
    if req and req.fake_llm:
        config_overrides = deep_merge(
            config_overrides,
            {
                "llm": {
                    "mode": "fake",
                    "provider": "fake",
                    "model": "fake-deterministic-blueprint-agent",
                    "runtime_model": None,
                    "require_live": False,
                }
            },
        )
    secret_environment = requested_secret_environment(req.secret_environment if req else None)
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    validate_blueprint_secret_environment(read_manifest_for_launch(bundle_root), secret_environment)
    env_overrides = runtime_blueprint_environment_overrides()
    env_overrides.update(fake_mode_environment_overrides(req))
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
    config_overrides = deep_merge(config_overrides, preflight.config_overrides)
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
        stable_job_id = req.job_id or generate_stable_job_id(blueprint["id"])
        submission_id = generate_job_definition_submission_id(stable_job_id)
        manifest_json, payloads = load_blueprint_bundle(
            repo_root,
            blueprint,
            run_id,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
            force=force,
            stable_job_id=stable_job_id,
            submission_id=submission_id,
            progress_callback=lambda message, detail, expectation: record_launch_progress(
                progress_id,
                "prepare_bundle",
                "running",
                message,
                {"run_id": run_id},
                label="Package workflow",
                detail=detail,
                expectation=expectation,
            ),
        )
        manifest_json = inject_declared_secret_environment(manifest_json, secret_environment)
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
    except Exception as exc:
        cleanup_blueprint_run_processes(run_id, reason="manifest_prepare_failed")
        record_launch_progress(
            progress_id,
            "prepare_bundle",
            "failed",
            str(exc) or "Job bundle preparation failed.",
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
        if not req.job_id:
            created = json.loads(
                state.client.create_stable_job(
                    manifest_json,
                    payloads,
                    job_id=stable_job_id,
                    resolved_configuration=config_overrides,
                )
            )
            stable_job_id = str(created["job_id"])
            definition_committed = True
        else:
            state.client.update_stable_job(
                stable_job_id,
                {"resolved_configuration": config_overrides}
                if config_overrides
                else {},
                manifest_json=manifest_json,
                payloads=payloads,
            )
            definition_committed = True
        started = json.loads(
            state.client.start_run(
                stable_job_id,
                run_id=run_id,
                inputs=config_overrides,
            )
        )
        execution_id = str(started["run_id"])
        write_blueprint_job_mapping(
            run_id,
            stable_job_id,
            execution_id,
            blueprint_id=blueprint["id"],
            blueprint_revision=blueprint.get("revision") or None,
            blueprint_source=source,
            blueprint_path=str(validate_blueprint_bundle(repo_root, blueprint)),
            monitor_manifest=manifest_without_secret_environment(manifest_json, secret_environment),
        )
        current_config = _current_config()
        start_background_event_relay_if_needed(
            repo_root,
            blueprint,
            run_id,
            execution_id,
            manifest_json,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
            grpc_target=getattr(current_config, "grpc_target", None),
            grpc_auth_token=getattr(current_config, "grpc_auth_token", None),
            grpc_timeout_seconds=getattr(current_config, "grpc_timeout_seconds", None),
        )
        record_launch_progress(
            progress_id,
            "submit",
            "completed",
            "Job submitted to the runtime.",
            {"run_id": execution_id, "job_id": stable_job_id},
            label="Submit runtime job",
            detail="The runtime accepted the job and live monitoring can begin.",
        )
        record_launch_progress(progress_id, "launch", "completed", "Launch complete.", {"run_id": execution_id, "job_id": stable_job_id})
        return {
            "job_id": stable_job_id,
            "id": execution_id,
            "run_id": execution_id,
            "status": "pending",
            "source": source,
            "blueprint": blueprint,
            "validation": validation,
            "model_install": model_install,
            "progress_id": progress_id,
            "progress_url": f"/api/v2/blueprints/launch/progress/{progress_id}" if progress_id else None,
        }
    except Exception as exc:
        if "submission_id" in locals() and not definition_committed:
            try:
                cleanup_job_definition_resources(manifest_json)
            except Exception:
                state.logger.exception(
                    "failed to clean prepared DockerWorker definition after launch failure",
                    extra={"submission_id": submission_id},
                )
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
        repo_root, blueprint = find_blueprint(_current_config(), req.blueprint_id)
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
        workflow_manifest = manifest.get("apiVersion") == "mn.workflow/v2" or manifest.get("kind") == "Workflow" or isinstance(manifest.get("workflow"), dict)
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
    if not isinstance(manifest, dict):
        return {}
    return expand_blueprint_manifest_if_source(bundle_root, manifest)


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
        "version": 2,
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
            "version": 2,
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
    if model_install.get("deferred") is True:
        return (
            f"Validated {len(models)} lazy runtime model polic"
            f"{'y' if len(models) == 1 else 'ies'}; selection and installation are deferred to first use."
        )
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
        "runtime_node_install": "runtime node install scheduled",
        "runtime_node_already_installed": "runtime node already present",
        "runtime_node_installed": "runtime node installed",
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
        "runtime_node_install",
        "runtime_node_already_installed",
        "runtime_node_installed",
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
        "Validating lazy runtime model policies.",
        label="Prepare runtime models",
        detail="Model selection and installation are deferred until the job first calls each model.",
        expectation="The first LLM, RAG, or OCR call may wait while its model is installed on the best compatible node.",
    )
    try:
        model_install = defer_blueprint_runtime_models(
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
            "Runtime model policy validation failed.",
            label="Prepare runtime models",
            detail="A runtime model declaration is invalid or the cluster cannot run it or its fallback.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch failed during model policy validation.",
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
            detail="A runtime model declaration is invalid or cluster feasibility could not be confirmed.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch failed during model policy validation.",
            run_details,
        )
        raise
    if not model_install.get("ok", True):
        record_launch_progress(
            progress_id,
            "model_install",
            "failed",
            "Runtime model policy validation failed.",
            {"model_install": model_install},
            label="Prepare runtime models",
            detail="A runtime model declaration is invalid or the cluster cannot run it or its fallback.",
            severity="error",
        )
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Blueprint launch failed during model policy validation.",
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
        detail="Runtime model declarations are valid; installation is deferred to first use.",
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
    config_patch = (
        model_install.get("config_overrides")
        if isinstance(model_install.get("config_overrides"), dict)
        else {}
    )
    return LaunchPreflight(
        model_install=model_install,
        env_overrides=env_overrides,
        config_overrides=config_patch,
    )


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
        "version": 2,
        "schema_version": "validation.report/v2",
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
