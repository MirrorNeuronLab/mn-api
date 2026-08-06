from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from mn_sdk import (
    AppError,
    BlueprintModelOps,
    BlueprintCatalogError,
    Client,
    ModelPrepareError,
    PayloadModelPackageError,
    build_prepare_runtime_model_request,
    call_prepare_runtime_model,
    cluster_provided_model,
    docker_model_runner_endpoint,
    docker_cli_path_environment,
    docker_model_installed,
    docker_model_name,
    expand_manifest_source,
    expand_manifest_model_service_requirements,
    is_manifest_source,
    load_model_catalog,
    load_model_ownership,
    load_model_remotes,
    make_validation_report,
    model_endpoints_json,
    model_service_tags as sdk_model_service_tags,
    blueprint_model_dependency_summary,
    install_model_entry,
    is_custom_model_requirement,
    is_payload_model_requirement,
    gateway_endpoint_map,
    prepare_job_submission,
    reapply_selected_workflow_placement,
    resolve_and_apply_workflow_placement,
    workflow_placement_mode,
    workflow_requires_single_node,
    payload_agent_root,
    package_payload_models,
    record_model_owner,
    required_blueprint_models,
    resolve_llm_environment,
    resolve_custom_model_placement,
    resolve_cluster_model_placement,
    remote_runtime_model_endpoint,
    resolve_requirement_entry,
    resolve_blueprint_payload_contract,
    resolve_model_endpoint,
    resolve_model_entry,
    run_hardware_requirements_validation,
    run_input_validation,
    run_model_validation,
    run_service_validation,
    runtime_model_prepare_timeout_seconds,
    sync_litellm_gateway,
    stage_payload_assets,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
    validate_resource_spec_issues,
    validate_service_spec_issues,
)
from mn_sdk.runtime_config import resolve_mn_home
from mn_sdk.run_store import (
    write_blueprint_job_mapping as sdk_write_blueprint_job_mapping,
)
from mn_sdk.model_preparation import (
    config_with_auto_runtime_model_profile,
    config_with_runtime_model_endpoints,
    config_with_runtime_model_fallbacks,
    config_with_runtime_model_profile,
    model_validation_inputs_with_prepared_models as sdk_model_validation_inputs_with_prepared_models,
    prepared_runtime_model_keys as sdk_prepared_runtime_model_keys,
    prepared_runtime_models_json as sdk_prepared_runtime_models_json,
    runtime_model_llm_environment,
)
from mn_sdk.blueprint_runtime import (
    add_mn_llm_aliases as sdk_add_mn_llm_aliases,
    adjust_llm_environment_for_node as sdk_adjust_llm_environment_for_node,
    apply_manifest_config_bindings as sdk_apply_manifest_config_bindings,
    blueprint_runtime_environment as sdk_blueprint_runtime_environment,
    config_path_get as sdk_config_path_get,
    config_to_environment as sdk_config_to_environment,
    deep_merge as sdk_deep_merge,
    inject_node_environment as sdk_inject_node_environment,
    load_blueprint_config as sdk_load_blueprint_config,
    load_blueprint_config_overwrites as sdk_load_blueprint_config_overwrites,
    merge_path_values as sdk_merge_path_values,
    set_manifest_path as sdk_set_manifest_path,
    shared_runs_root as sdk_shared_runs_root,
    with_shared_run_store_config as sdk_with_shared_run_store_config,
)
from mn_sdk.blueprint_source import (
    as_dict as sdk_as_dict,
    as_list as sdk_as_list,
    blueprint_bundle_root as sdk_blueprint_bundle_root,
    blueprint_repo_root as sdk_blueprint_repo_root,
    cached_git_blueprint_repo_path as sdk_cached_git_blueprint_repo_path,
    category_slug as sdk_category_slug,
    clone_git_blueprint_repo as sdk_clone_git_blueprint_repo,
    ensure_git_blueprint_repo as sdk_ensure_git_blueprint_repo,
    enrich_blueprint_from_manifest as sdk_enrich_blueprint_from_manifest,
    filter_blueprints_by_category as sdk_filter_blueprints_by_category,
    find_blueprint as sdk_find_blueprint,
    is_git_repo_url as sdk_is_git_repo_url,
    load_active_blueprint_catalog as sdk_load_active_blueprint_catalog,
    load_blueprint_categories as sdk_load_blueprint_categories,
    load_optional_manifest as sdk_load_optional_manifest,
    manifest_init_config_review as sdk_manifest_init_config_review,
    normalize_blueprint as sdk_normalize_blueprint,
    normalize_category_name as sdk_normalize_category_name,
    run_git as sdk_run_git,
)
from mn_sdk.context_engine import blueprint_requires_context_engine
from mn_sdk.runtime_modules import (
    RuntimeModuleInstallError,
    ensure_runtime_modules_for_manifest,
    runtime_path_environment as sdk_runtime_path_environment,
)
from mn_sdk.skill_runtime import (
    prepare_skill_runtime_for_manifest as sdk_prepare_skill_runtime_for_manifest,
    stage_skill_runtime_payloads_for_manifest as sdk_stage_skill_runtime_payloads_for_manifest,
)
from mn_sdk.skill_dependencies import skill_dependency_package_names
from mn_sdk.submission_preparation import (
    ensure_blueprint_support_sdk_build_context_uploads,
    inject_skill_dependency_python_environments,
    inject_localized_hostlocal_python_environments,
    localize_agent_dependencies_for_dev,
    localize_skill_dependencies_for_dev,
    lower_manifest_topology_for_runtime_submission,
    manifest_nodes as sdk_submission_manifest_nodes,
    normalize_host_local_uploads,
    prepare_manifest_submission,
    refresh_embedded_blueprint_config,
    release_skill_dependency_runtime_environment,
    stage_blueprint_support_payloads_for_manifest,
    stage_skill_dependency_payloads_for_manifest,
    stage_upload_path_payloads_for_manifest,
    strip_docker_model_runner_placement_requirements,
)
from mn_sdk.blueprint_support import (
    make_run_id,
    render_manifest_agent_templates,
    stage_local_input_payloads_for_manifest as stage_sdk_local_input_payloads,
)

from mn_api.config import (
    ApiConfig,
    config_bool,
    config_float,
    config_optional_value,
    config_path,
    config_value,
    runtime_env_values,
    subprocess_environment,
)

BLUEPRINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,220}$")
CATEGORY_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
DEFAULT_CATEGORY = "General"
DEFAULT_BLUEPRINT_REPO_CACHE = "~/.cache/mirror-neuron/blueprint-repos"
PRE_LAUNCH_SCRIPT = Path("scripts/pre-launch.sh")
POST_LAUNCH_SCRIPT = Path("scripts/post-launch.sh")
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
UNMAPPED_RUN_STALE_SECONDS = 120
PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
BLUEPRINT_RUNTIME_ENV_KEYS = (
    "MN_BLUEPRINT_WEB_UI_BIND_HOST",
    "MN_BLUEPRINT_WEB_UI_HOST",
    "MN_BLUEPRINT_WEB_UI_PUBLIC_HOST",
    "MN_BLUEPRINT_WEB_UI_BASE_URL",
    "MN_BLUEPRINT_WEB_UI_PUBLISH_HOST",
    "MN_BLUEPRINT_WEB_UI_PORT_START",
    "MN_BLUEPRINT_WEB_UI_PORT_END",
    "MN_BLUEPRINT_WEB_UI_PORT_ALLOCATION_MODE",
    "MN_BLUEPRINT_WEB_UI_API_BASE_URL",
    "MN_BLUEPRINT_RUN_EVENTS_URL",
    "MN_API_BASE_URL",
    "MN_API_HOST",
    "MN_API_PORT",
    "MN_API_TOKEN",
)


def _catalog_http_exception(exc: BlueprintCatalogError) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status_code", 500), detail=getattr(exc, "detail", str(exc)))


def workspace_root() -> Path:
    for name in ("MN_WORKSPACE_ROOT",):
        value = config_value(name)
        if value:
            return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def runtime_path_environment() -> Dict[str, str]:
    base_env = subprocess_environment()
    # Match mn-cli's blueprint runner: runtime path discovery starts from the
    # live process environment so service/desktop launch PATH differences are
    # preserved and then repaired with known Docker CLI locations.
    base_env.update(os.environ)
    env = sdk_runtime_path_environment(env=base_env, workspace_root=workspace_root())
    docker_env = docker_cli_runtime_environment(base_env)
    env["PATH"] = docker_env.get("PATH", config_value("PATH"))
    if docker_env.get("MN_DOCKER_BIN"):
        env["MN_DOCKER_BIN"] = docker_env["MN_DOCKER_BIN"]
    return env


def runtime_process_environment() -> Dict[str, str]:
    env = subprocess_environment()
    env.update(runtime_path_environment())
    return env


def docker_cli_runtime_environment(base_env: Dict[str, str] | None = None) -> Dict[str, str]:
    env = docker_cli_path_environment(base_env)
    path = env.get("PATH") or config_value("PATH")
    docker_bin = str(env.get("MN_DOCKER_BIN") or "").strip()
    if not docker_bin:
        docker_bin = shutil.which("docker", path=path) or ""
    if docker_bin:
        docker_dir = str(Path(docker_bin).expanduser().parent)
        path = sdk_merge_path_values(docker_dir, path)
        env["MN_DOCKER_BIN"] = docker_bin
    env["PATH"] = path
    return env


@contextmanager
def temporary_process_environment(env: Dict[str, str]):
    updates = {str(key): str(value) for key, value in env.items() if value is not None}
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def ensure_runtime_modules_for_submission(
    manifest: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    try:
        env = {**os.environ, **runtime_path_environment()}
        return ensure_runtime_modules_for_manifest(manifest, config, env=env, workspace_root=workspace_root())
    except RuntimeModuleInstallError as exc:
        raise AppError(
            "MN_EXECUTION_FAILED",
            "A required runtime module could not be installed automatically.",
            internal_message=str(exc),
            hint="Check the API logs and runtime module configuration, then try again.",
            http_status=500,
            cause=exc,
        ) from exc


def as_dict(value: Any) -> Dict[str, Any]:
    return sdk_as_dict(value)


def as_list(value: Any) -> list[Any]:
    return sdk_as_list(value)


def string_env_values(values: Dict[str, Any] | None) -> Dict[str, str]:
    return {str(key): str(value) for key, value in (values or {}).items() if value is not None}


def runtime_blueprint_environment_overrides() -> Dict[str, str]:
    runtime_env = runtime_env_values()
    overrides: Dict[str, str] = {}
    for key in BLUEPRINT_RUNTIME_ENV_KEYS:
        value = config_value(key, runtime_env=runtime_env)
        if isinstance(value, str) and value.strip():
            overrides[key] = value.strip()
    return overrides


def normalize_category_name(value: Any) -> str:
    return sdk_normalize_category_name(value)


def category_slug(value: Any) -> str:
    return sdk_category_slug(value)


def is_git_repo_url(value: str) -> bool:
    return sdk_is_git_repo_url(value)


def validate_blueprint_id(blueprint_id: str) -> None:
    if not BLUEPRINT_ID_PATTERN.fullmatch(blueprint_id):
        raise HTTPException(status_code=400, detail="invalid blueprint id")


def validate_run_id(run_id: str) -> None:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")


def sanitize_blueprint_id(value: Any, fallback: str = "local_blueprint") -> str:
    raw = str(value or "").strip() or fallback
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_.-")
    return (sanitized or fallback)[:160]


def create_blueprint_run_id(blueprint_id: str) -> str:
    return make_run_id(blueprint_id)


def cached_git_repo_path(repo_url: str) -> Path:
    configured_cache = (
        config_path("MN_BLUEPRINT_REPO_CACHE", default=DEFAULT_BLUEPRINT_REPO_CACHE)
        or Path(DEFAULT_BLUEPRINT_REPO_CACHE).expanduser()
    )
    return sdk_cached_git_blueprint_repo_path(repo_url, cache_root=configured_cache)


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return sdk_run_git(args)


def clone_git_blueprint_repo(repo_url: str, target: Path) -> None:
    sdk_clone_git_blueprint_repo(repo_url, target)


def ensure_git_blueprint_repo(repo_url: str) -> Path:
    try:
        return sdk_ensure_git_blueprint_repo(repo_url, cache_root=cached_git_repo_path(repo_url).parent)
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def blueprint_repo_root(config: ApiConfig) -> Path:
    try:
        return sdk_blueprint_repo_root(config, github_resolver=ensure_git_blueprint_repo)
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def load_blueprint_catalog(config: ApiConfig) -> tuple[Path, list[Dict[str, Any]]]:
    try:
        catalog = sdk_load_active_blueprint_catalog(config)
        return catalog.repo_root, catalog.blueprints
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def normalize_blueprint(entry: Any) -> Optional[Dict[str, Any]]:
    return sdk_normalize_blueprint(entry)


def load_blueprint_categories(repo_root: Path, blueprints: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    try:
        return sdk_load_blueprint_categories(repo_root, blueprints)
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def filter_blueprints_by_category(
    blueprints: list[Dict[str, Any]],
    category: str | None,
) -> list[Dict[str, Any]]:
    return sdk_filter_blueprints_by_category(blueprints, category)


def find_blueprint(config: ApiConfig, blueprint_id: str) -> tuple[Path, Dict[str, Any]]:
    validate_blueprint_id(blueprint_id)
    repo_root, blueprints = load_blueprint_catalog(config)
    try:
        return sdk_find_blueprint(repo_root, blueprints, blueprint_id)
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def blueprint_bundle_root(repo_root: Path, blueprint: Dict[str, Any]) -> Path:
    try:
        return sdk_blueprint_bundle_root(repo_root, blueprint)
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def validate_blueprint_bundle(repo_root: Path, blueprint: Dict[str, Any]) -> Path:
    bundle_root = blueprint_bundle_root(repo_root, blueprint)
    if not bundle_root.is_dir():
        raise HTTPException(status_code=500, detail="blueprint bundle directory was not found")
    if not (bundle_root / "manifest.json").is_file():
        raise HTTPException(status_code=500, detail="blueprint bundle manifest.json was not found")
    return bundle_root


def expand_blueprint_manifest_if_source(bundle_root: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    if is_manifest_source(manifest):
        return expand_manifest_source(manifest, root_dir=bundle_root)
    return manifest


def package_payload_models_for_api(
    bundle_root: Path,
    manifest: Dict[str, Any],
) -> list[dict[str, str]]:
    try:
        return package_payload_models(
            bundle_root,
            manifest,
            env=runtime_process_environment(),
        )
    except PayloadModelPackageError as exc:
        raise AppError(
            "MN_EXECUTION_FAILED",
            "A blueprint payload model could not be prepared.",
            internal_message=str(exc),
            hint="Check Docker Model Runner and the runtime.models payload declaration.",
            http_status=500,
            cause=exc,
        ) from exc


def install_blueprint_runtime_models(
    repo_root: Path,
    blueprint: Dict[str, Any],
    *,
    force: bool = False,
    config_overrides: Dict[str, Any] | None = None,
    service_progress: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> Dict[str, Any]:
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    try:
        manifest = json.loads((bundle_root / "manifest.json").read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=500, detail="blueprint manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_root, manifest)
    resolve_blueprint_payload_contract(manifest, bundle_root)
    package_payload_models_for_api(bundle_root, manifest)
    base_config = load_blueprint_config(
        bundle_root, config_overrides=config_overrides
    ) or {}
    service_results: list[dict[str, Any]] = []
    blueprint_id = str(blueprint.get("id") or "")
    blueprint_revision = str(blueprint.get("revision") or "")
    catalog = load_model_catalog()
    config = config_with_auto_runtime_model_profile(
        base_config,
        catalog=catalog,
        resolve_cluster_model=resolve_runtime_cluster_model_for_api,
    )
    summary = blueprint_model_dependency_summary(
        blueprint_id=blueprint_id,
        blueprint_revision=blueprint_revision,
        bundle_root=bundle_root,
        manifest=manifest,
        config=config,
        install_source=str(repo_root),
        force=force,
        ops=BlueprintModelOps(
            load_model_catalog=lambda: catalog,
            required_blueprint_models=required_blueprint_models,
            load_model_ownership=load_model_ownership,
            resolve_model_entry=resolve_model_entry,
            docker_model_name=docker_model_name,
            cluster_provided_model=cluster_provided_model,
            record_model_owner=record_model_owner,
            model_installed=docker_model_installed,
            install_model_entry=install_model_entry,
            resolve_model_endpoint=resolve_runtime_model_endpoint_for_api,
            resolve_cluster_model=resolve_runtime_cluster_model_for_api,
            install_cluster_model=install_runtime_cluster_model_for_api,
        ),
    )
    errors = list(summary.get("errors") or [])
    if not errors and blueprint_requires_context_engine(manifest, config):
        if service_progress is not None:
            service_progress("context_engine_needed", None)
        context_result = ensure_context_engine_for_blueprint(bundle_root, force=force)
        service_results.append(context_result)
        if context_result.get("status") == "failed":
            if service_progress is not None:
                service_progress("context_engine_failed", context_result)
            errors.append(str(context_result.get("error") or "context engine setup failed"))
        elif service_progress is not None:
            service_progress("context_engine_ready", context_result)
    if not errors:
        try:
            endpoints = sync_runtime_model_gateways_for_api(summary)
        except Exception as exc:
            errors.append(f"LiteLLM gateway synchronization failed: {exc}")
            endpoints = {}
    else:
        endpoints = {}
    env: dict[str, str] = {}
    if endpoints:
        summary["endpoints"] = endpoints
        env["MN_MODEL_ENDPOINTS_JSON"] = model_endpoints_json(endpoints)
    models = summary.get("models") if isinstance(summary.get("models"), list) else []
    prepared_json = sdk_prepared_runtime_models_json({"models": models})
    if prepared_json:
        env["MN_PREPARED_RUNTIME_MODELS_JSON"] = prepared_json
    materialized_config = config_with_runtime_model_endpoints(config, summary)
    materialized_config = config_with_runtime_model_fallbacks(
        materialized_config, summary
    )
    materialized_config = config_with_runtime_model_profile(materialized_config)
    materialized_config = config_with_runtime_model_endpoints(
        materialized_config, summary
    )
    if materialized_config != base_config:
        env["MN_BLUEPRINT_CONFIG_JSON"] = json.dumps(
            materialized_config, sort_keys=True
        )
        env.update(runtime_model_llm_environment(materialized_config))
    if blueprint_requests_default_llm(base_config):
        env["MN_LLM_MODEL"] = "default"
        env["LITELLM_MODEL"] = "default"
    return {
        "ok": not errors,
        "models": models,
        "services": service_results,
        "endpoints": endpoints,
        "env": env,
        "config_overrides": (
            materialized_config if materialized_config != base_config else None
        ),
        "errors": errors,
    }


def defer_blueprint_runtime_models(
    repo_root: Path,
    blueprint: Dict[str, Any],
    *,
    config_overrides: Dict[str, Any] | None = None,
    force: bool = False,
    service_progress: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> Dict[str, Any]:
    """Validate model declarations and defer DMR preparation until first use."""

    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    try:
        manifest = json.loads((bundle_root / "manifest.json").read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=500, detail="blueprint manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_root, manifest)
    resolve_blueprint_payload_contract(manifest, bundle_root)
    package_payload_models_for_api(bundle_root, manifest)
    config = load_blueprint_config(bundle_root, config_overrides=config_overrides) or {}
    catalog = load_model_catalog()
    resource_report = runtime_resource_report()
    models: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for requirement in required_blueprint_models(manifest, config, catalog=catalog):
        requested = str(requirement.get("model") or requirement.get("name") or "").strip()
        if not requested:
            continue
        try:
            entry = resolve_requirement_entry(
                requirement,
                catalog=catalog,
                catalog_resolver=resolve_model_entry,
            )
        except Exception as exc:
            errors.append(f"Unknown runtime model {requested}: {exc}")
            continue
        key = str(entry.get("id") or requested).lower()
        if key in seen:
            continue
        seen.add(key)
        if (
            str(entry.get("provider") or "docker_model_runner")
            == "docker_model_runner"
            and isinstance(entry.get("requirements"), dict)
            and entry.get("requirements")
            and not deferred_runtime_model_is_feasible(
                entry,
                resource_report=resource_report,
                catalog=catalog,
            )
        ):
            errors.append(
                "No healthy cluster node can run runtime model "
                f"{entry.get('id') or requested} or its catalog fallback."
            )
        logical = (
            "default"
            if requirement.get("default") is True or requested.lower() == "default"
            else requested
        )
        policy = (
            ["nemotron3", "gemma4:e2b"]
            if logical == "default"
            else [str(entry.get("id") or requested)]
        )
        models.append(
            {
                "id": logical,
                "name": str(requirement.get("name") or logical),
                "model": logical,
                "runtime_model": str(entry.get("model") or requested),
                "provider": str(entry.get("provider") or "docker_model_runner"),
                "status": (
                    "packaged_payload"
                    if is_payload_model_requirement(requirement)
                    else "deferred_runtime_install"
                ),
                "install_policy": (
                    "payload"
                    if is_payload_model_requirement(requirement)
                    else "on_first_model_call"
                ),
                "selection_policy": policy,
                "source": str(
                    requirement.get("path")
                    or requirement.get("manifest_path")
                    or "config"
                ),
            }
        )
    env: dict[str, str] = {}
    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    llm_provider = str(llm.get("provider") or "docker_model_runner").strip().lower()
    if models and llm_provider in {"", "docker_model_runner", "docker-model-runner", "dmr"}:
        env.update(
            {
                "MN_RUNTIME_MODEL_MANAGED": "1",
                "MN_LLM_PROVIDER": "docker_model_runner",
                "LITELLM_PROVIDER": "docker_model_runner",
                "MN_LLM_API_BASE": "auto",
                "LITELLM_API_BASE": "auto",
            }
        )
    if blueprint_requests_default_llm(config):
        env["MN_LLM_MODEL"] = "default"
        env["LITELLM_MODEL"] = "default"
    service_results: list[dict[str, Any]] = []
    if not errors and blueprint_requires_context_engine(manifest, config):
        if service_progress is not None:
            service_progress("context_engine_needed", None)
        context_result = ensure_context_engine_for_blueprint(bundle_root, force=force)
        service_results.append(context_result)
        if context_result.get("status") == "failed":
            errors.append(str(context_result.get("error") or "context engine setup failed"))
            if service_progress is not None:
                service_progress("context_engine_failed", context_result)
        elif service_progress is not None:
            service_progress("context_engine_ready", context_result)
    return {
        "ok": not errors,
        "deferred": True,
        "models": models,
        "services": service_results,
        "endpoints": {},
        "env": env,
        "config_overrides": None,
        "errors": errors,
    }


def deferred_runtime_model_is_feasible(
    entry: dict[str, Any],
    *,
    resource_report: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> bool:
    if resolve_cluster_model_placement(entry, resource_report=resource_report):
        return True
    fallback_ref = str(entry.get("fallback_model") or "").strip()
    if not fallback_ref:
        return False
    try:
        fallback = resolve_model_entry(fallback_ref, catalog=catalog)
    except Exception:
        return False
    return bool(
        resolve_cluster_model_placement(fallback, resource_report=resource_report)
    )


def blueprint_requests_default_llm(config: dict[str, Any]) -> bool:
    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    return str(llm.get("model") or "").strip().lower() == "default"


def sync_runtime_model_gateways_for_api(
    summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Publish prepared DMR routes to every healthy runtime gateway.

    Worker manifests always use their node-local LiteLLM proxy.  Gateways are
    reconciled with direct DMR routes here so a HostLocal worker can follow a
    model installed on another node without embedding a static remote URL in
    the submitted blueprint.
    """

    upstream_endpoints = summary.get("endpoints")
    upstream = (
        dict(upstream_endpoints)
        if isinstance(upstream_endpoints, dict)
        else {}
    )
    upstream.update(local_runtime_model_endpoints_for_api(summary))
    if not upstream:
        return {}

    restart = str(os.getenv("MN_LITELLM_GATEWAY_RESTART", "true")).strip().lower()
    restart_enabled = restart not in {"0", "false", "no", "off"}
    gateway = sync_litellm_gateway(
        runtime_endpoints=upstream,
        restart=restart_enabled,
    )
    fanout_runtime_model_gateways_for_api(upstream, restart=restart_enabled)
    summary["gateway"] = gateway
    return gateway_endpoint_map(upstream)


def local_runtime_model_endpoints_for_api(
    summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build direct DMR routes for models prepared on this API's node."""

    endpoints: dict[str, dict[str, Any]] = {}
    prepared_statuses = {"installed", "already_installed", "fallback_model"}
    for item in summary.get("models") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") not in prepared_statuses:
            continue
        if str(item.get("provider") or "docker_model_runner") != "docker_model_runner":
            continue
        endpoint = item.get("endpoint") if isinstance(item.get("endpoint"), dict) else {}
        if endpoint and str(endpoint.get("source") or "") not in {"", "local-dmr"}:
            continue
        effective = item.get("effective") if isinstance(item.get("effective"), dict) else {}
        model_ref = str(
            effective.get("id")
            or effective.get("model")
            or item.get("id")
            or item.get("model")
            or ""
        ).strip()
        if not model_ref:
            continue
        try:
            entry = resolve_model_entry(model_ref)
        except Exception:
            entry = {
                "id": model_ref,
                "provider": "docker_model_runner",
                "model": str(effective.get("model") or item.get("model") or model_ref),
                "api_model": str(effective.get("model") or item.get("model") or model_ref),
            }
        direct = docker_model_runner_endpoint(entry, source="local-dmr")
        for key in runtime_model_route_keys_for_api(item, entry, effective):
            endpoints[key] = direct
    return endpoints


def runtime_model_route_keys_for_api(
    item: dict[str, Any],
    entry: dict[str, Any],
    effective: dict[str, Any],
) -> set[str]:
    keys = {
        str(item.get("id") or "").strip(),
        str(item.get("model") or "").strip(),
        str(effective.get("id") or "").strip(),
        str(effective.get("model") or "").strip(),
        str(entry.get("id") or "").strip(),
        str(entry.get("model") or "").strip(),
        str(entry.get("api_model") or "").strip(),
    }
    for aliases in (entry.get("aliases"), entry.get("route_aliases")):
        if isinstance(aliases, list):
            keys.update(str(alias).strip() for alias in aliases if str(alias).strip())
    return {key for key in keys if key}


def fanout_runtime_model_gateways_for_api(
    runtime_endpoints: dict[str, dict[str, Any]],
    *,
    restart: bool,
) -> None:
    """Require every healthy remote proxy to accept the dynamic route map."""

    from mn_api import state

    try:
        system_summary = json.loads(state.client.get_system_summary())
    except Exception as exc:
        raise RuntimeError(f"could not inspect runtime nodes for LiteLLM sync: {exc}") from exc
    nodes = system_summary.get("nodes") if isinstance(system_summary, dict) else None
    if not isinstance(nodes, list):
        raise RuntimeError("runtime node metadata is invalid for LiteLLM sync")
    current_config = state.refresh_config_from_env()
    for node in nodes:
        if not api_gateway_sync_eligible_node(node):
            continue
        node_name = str(node.get("name") or node.get("node") or "").strip()
        if bool(node.get("self") is True or node.get("self?") is True):
            continue
        native = native_sdk_grpc_for_api_node(node)
        target, _host = native_sdk_target_for_api_node(native)
        if not target:
            raise RuntimeError(
                f"runtime node {node_name} does not advertise native SDK gRPC for LiteLLM sync"
            )
        runtime_client = Client(
            target=target,
            timeout=runtime_model_prepare_timeout_seconds(),
            auth_token=getattr(current_config, "grpc_auth_token", None),
            admin_token=getattr(current_config, "grpc_admin_token", None),
        )
        try:
            response = runtime_client.sync_litellm_gateway(
                {
                    "node": node_name,
                    "runtime_endpoints": runtime_endpoints,
                    "restart": restart,
                    "source": "mn-api-runtime-endpoint-fanout",
                }
            )
            decoded = json.loads(response) if isinstance(response, str) else response
        except Exception as exc:
            raise RuntimeError(f"could not sync LiteLLM gateway on {node_name}: {exc}") from exc
        if not isinstance(decoded, dict) or str(decoded.get("status") or "").lower() in {
            "failed",
            "error",
        }:
            raise RuntimeError(f"LiteLLM gateway synchronization failed on {node_name}")


def api_gateway_sync_eligible_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    name = str(node.get("name") or node.get("node") or "").strip()
    status = str(node.get("status") or "").strip().lower()
    return bool(
        name
        and status in {"", "healthy", "joining"}
        and node.get("scheduling_eligible") is not False
        and not node.get("drain")
        and not node.get("maintenance")
    )


def native_sdk_grpc_for_api_node(node: dict[str, Any]) -> dict[str, Any]:
    for candidate in (
        node.get("native_sdk_grpc"),
        (node.get("hardware") or {}).get("native_sdk_grpc")
        if isinstance(node.get("hardware"), dict)
        else None,
        (node.get("node_info") or {}).get("native_sdk_grpc")
        if isinstance(node.get("node_info"), dict)
        else None,
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def native_sdk_target_for_api_node(native: dict[str, Any]) -> tuple[str, str]:
    target = str(native.get("target") or "").strip()
    host = str(native.get("host") or "").strip()
    port = str(native.get("port") or "").strip()
    if target and (not host or not port) and ":" in target:
        host, port = target.rsplit(":", 1)
    if not target and host and port:
        target = f"{host}:{port}"
    return target, host


def resolve_runtime_cluster_model_for_api(
    *,
    requirement: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    from mn_api import state

    try:
        system_summary = json.loads(state.client.get_system_summary())
    except Exception as exc:
        if is_custom_model_requirement(requirement):
            raise ModelPrepareError(
                "model.custom_cluster_inspection_failed",
                f"could not inspect cluster nodes for custom model placement: {exc}",
                stage="placement",
                safe_message="Could not inspect runtime nodes for custom model placement.",
            ) from exc
        return None
    if not isinstance(system_summary, dict):
        if is_custom_model_requirement(requirement):
            raise ModelPrepareError(
                "model.custom_cluster_inspection_failed",
                "runtime system summary is not a JSON object",
                stage="placement",
                safe_message="Runtime node metadata is invalid for custom model placement.",
            )
        return None

    resource_report = runtime_resource_report()
    if is_custom_model_requirement(requirement):
        placement = resolve_custom_model_placement(
            resource_report=resource_report,
            system_summary=system_summary,
        )
        state.logger.info(
            "custom_model_node_selected model=%s node=%s selection=%s",
            entry.get("model"),
            placement.get("node"),
            json.dumps(placement.get("selection") or {}, separators=(",", ":"), sort_keys=True),
        )
        return enrich_api_cluster_model_placement(placement, system_summary)

    placement = resolve_cluster_model_placement(
        entry, resource_report=resource_report
    )
    if placement:
        return enrich_api_cluster_model_placement(placement, system_summary)

    fallback_ref = str(entry.get("fallback_model") or "").strip()
    if not fallback_ref:
        return None
    try:
        fallback_entry = resolve_model_entry(fallback_ref)
    except Exception:
        return None
    fallback_placement = resolve_cluster_model_placement(
        fallback_entry, resource_report=resource_report
    )
    if not fallback_placement:
        return None
    resolved = enrich_api_cluster_model_placement(fallback_placement, system_summary)
    resolved.update(
        {
            "source": "cluster_fallback",
            "status": "fallback_model",
            "fallback_entry": fallback_entry,
            "fallback_reason": "no_capable_node_for_preferred_model",
        }
    )
    return resolved


def enrich_api_cluster_model_placement(
    placement: dict[str, Any], system_summary: dict[str, Any]
) -> dict[str, Any]:
    """Attach the target node's native gRPC and DMR-reachable host metadata."""

    node_name = str(placement.get("node") or "").strip()
    nodes = system_summary.get("nodes") if isinstance(system_summary, dict) else []
    system_node = next(
        (
            node
            for node in nodes or []
            if isinstance(node, dict)
            and str(node.get("name") or node.get("node") or "").strip() == node_name
        ),
        {},
    )
    if not isinstance(system_node, dict):
        system_node = {}
    native = native_sdk_grpc_for_api_node(system_node)
    target, native_host = native_sdk_target_for_api_node(native)
    advertised_host = str(
        system_node.get("grpc_host") or system_node.get("address") or native_host or ""
    ).strip()
    enriched = dict(placement)
    enriched["local"] = bool(
        system_node.get("self") is True or system_node.get("self?") is True
    )
    if native:
        enriched["native_sdk_grpc"] = {
            **native,
            **({"target": target} if target else {}),
            **({"host": native_host} if native_host else {}),
        }
    if advertised_host:
        enriched["dmr_host"] = advertised_host
    return enriched


def install_runtime_cluster_model_for_api(
    *,
    requirement: dict[str, Any],
    entry: dict[str, Any],
    model: dict[str, Any],
    cluster: dict[str, Any],
    backend: str,
    context_size: Any,
    force: bool,
) -> dict[str, Any]:
    from mn_api import state

    node = str(cluster.get("node") or "").strip()
    native = cluster.get("native_sdk_grpc") if isinstance(cluster.get("native_sdk_grpc"), dict) else {}
    target = str(native.get("target") or "").strip()
    host = str(cluster.get("dmr_host") or native.get("host") or "").strip()
    port = str(native.get("port") or "").strip()
    if not target and host and port:
        target = f"{host}:{port}"
    local = bool(cluster.get("local") is True)
    if not node:
        raise RuntimeError("runtime model placement did not return a target node")
    if local:
        runtime_client = state.client
    else:
        if not target or not host:
            raise RuntimeError(
                "runtime model placement returned incomplete native SDK gRPC metadata"
            )
        current_config = state.refresh_config_from_env()
        runtime_client = Client(
            target=target,
            timeout=runtime_model_prepare_timeout_seconds(),
            auth_token=getattr(current_config, "grpc_auth_token", None),
            admin_token=getattr(current_config, "grpc_admin_token", None),
        )
    prepare_payload = build_prepare_runtime_model_request(
        requirement=requirement,
        entry=entry,
        model=model,
        node=node,
        backend=backend,
        context_size=context_size,
        force=force,
        source="mn-api",
    )
    payload = call_prepare_runtime_model(runtime_client, prepare_payload, logger=state.logger)
    if host:
        endpoint = remote_runtime_model_endpoint(
            entry=entry,
            node=node,
            node_host=host,
            payload=payload,
        )
    else:
        endpoint = docker_model_runner_endpoint(
            entry,
            node=node,
            source="local-dmr",
        )
    return {
        "install": payload,
        "endpoint": endpoint,
    }


def resolve_runtime_model_endpoint_for_api(*, requirement: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    model = str(requirement.get("model") or entry.get("id") or "").strip()
    config = requirement.get("config") if isinstance(requirement.get("config"), dict) else {}
    services = resolve_model_services_for_requirement(entry)
    try:
        return resolve_model_endpoint(
            model,
            config=config,
            entry=entry,
            services=services,
            remotes=load_model_remotes(),
        )
    except Exception:
        from mn_api import state

        state.logger.exception("runtime_model_endpoint_resolution_failed model=%s", model)
        return None


def prepared_runtime_models_json(results: list[dict[str, Any]] | dict[str, Any]) -> str:
    summary = results if isinstance(results, dict) else {"models": results}
    return sdk_prepared_runtime_models_json(summary)


def prepared_runtime_model_keys(model_install_summary: dict[str, Any] | None) -> set[str]:
    return sdk_prepared_runtime_model_keys(model_install_summary)


def prepared_runtime_model_keys_from_env(env: dict[str, str] | None) -> set[str]:
    raw = str((env or {}).get("MN_PREPARED_RUNTIME_MODELS_JSON") or "").strip()
    if not raw:
        return set()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(decoded, list):
        return set()
    return {str(item).strip() for item in decoded if str(item).strip()}


def prepared_model_installed_resolver(env: dict[str, str] | None):
    prepared = prepared_runtime_model_keys_from_env(env)
    if not prepared:
        return None

    def resolver(model_name: str, requirement: dict[str, Any]) -> bool:
        keys = {
            str(model_name or "").strip(),
            str(requirement.get("model") or "").strip(),
            str(requirement.get("runtime_model") or "").strip(),
            str(requirement.get("name") or "").strip(),
        }
        if any(key and key in prepared for key in keys):
            return True
        return docker_model_installed(model_name)

    return resolver


def model_validation_inputs_with_prepared_models(
    manifest: dict[str, Any],
    config: dict[str, Any],
    prepared: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    return sdk_model_validation_inputs_with_prepared_models(
        manifest,
        config,
        prepared=prepared,
    )


def model_config_matches_prepared(config: dict[str, Any], prepared: set[str]) -> bool:
    values = {
        str(config.get("runtime_model") or "").strip(),
        str(config.get("model") or "").strip(),
        str(config.get("model_alias") or "").strip(),
    }
    return any(model_match_keys(value) & prepared for value in values if value)


def model_match_keys(model: str) -> set[str]:
    value = str(model or "").strip().lower().replace("_", "-")
    if not value:
        return set()
    keys = {value}
    for prefix in ("docker.io/", "registry-1.docker.io/"):
        if value.startswith(prefix):
            keys.add(value[len(prefix) :])
    for candidate in list(keys):
        if candidate.startswith("ai/"):
            keys.add(candidate[3:])
        elif "/" not in candidate:
            keys.add(f"ai/{candidate}")
    for candidate in list(keys):
        no_latest = candidate.removesuffix(":latest")
        keys.add(no_latest)
        if no_latest.startswith("ai/"):
            keys.add(no_latest[3:])
        elif "/" not in no_latest:
            keys.add(f"ai/{no_latest}")
    return keys


def resolve_model_services_for_requirement(entry: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from mn_api import state
    except Exception:
        return []
    services: list[dict[str, Any]] = []
    for tag in model_service_tags(entry):
        try:
            response = state.client.resolve_service(
                "docker-model-runner",
                tags=[tag],
                passing_only=True,
            )
            decoded = json.loads(response)
        except Exception:
            continue
        for service in decoded.get("services") or []:
            if isinstance(service, dict) and service not in services:
                services.append(service)
    return services


def model_service_tags(entry: dict[str, Any]) -> list[str]:
    return sdk_model_service_tags(entry)


def ensure_context_engine_for_blueprint(bundle_root: Path, *, force: bool = False) -> dict[str, Any]:
    direct_result = ensure_context_engine_runtime_direct(force=force)
    if direct_result is not None:
        return direct_result

    command = mn_base_command() + ["runtime", "ensure-context-engine"]
    if force:
        command.append("--force")
    env = runtime_process_environment()
    env.setdefault("MN_DEBUG", "1")
    result = subprocess.run(
        command,
        cwd=str(bundle_root),
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
        env=env,
    )
    base_result: dict[str, Any] = {
        "name": "membrane-context-engine",
        "status": "ready" if result.returncode == 0 else "failed",
        "command": " ".join(command),
    }
    if result.returncode != 0:
        base_result["error"] = (result.stderr or result.stdout or "context engine setup failed").strip()
    return base_result


def ensure_context_engine_runtime_direct(*, force: bool = False) -> dict[str, Any] | None:
    try:
        from mn_cli.server_cmds import ensure_context_engine_runtime
    except Exception:
        return None
    try:
        with temporary_process_environment(runtime_process_environment()):
            summary = ensure_context_engine_runtime(force=force)
    except Exception as exc:
        return {
            "name": "membrane-context-engine",
            "status": "failed",
            "error": str(exc) or "context engine setup failed",
        }
    return {
        "name": "membrane-context-engine",
        "status": "ready",
        **({key: value for key, value in summary.items() if value is not None} if isinstance(summary, dict) else {}),
    }


def local_blueprint_from_path(path: str) -> tuple[Path, Dict[str, Any]]:
    bundle_root = Path(path).expanduser().resolve()
    if not bundle_root.is_dir():
        raise HTTPException(status_code=400, detail="blueprint path is not a directory")
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=400, detail="blueprint path must contain manifest.json")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="blueprint manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="blueprint manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_root, manifest)

    metadata = as_dict(manifest.get("metadata"))
    identity = as_dict(manifest.get("identity"))
    workflow_manifest = manifest.get("apiVersion") == "mn.workflow/v1" or manifest.get("kind") == "Workflow" or isinstance(manifest.get("workflow"), dict)
    raw_id = (
        metadata.get("blueprint_id")
        or identity.get("blueprint_id")
        or manifest.get("blueprint_id")
        or manifest.get("id")
        or manifest.get("workflow_id")
        or (None if workflow_manifest else manifest.get("graph_id"))
        or bundle_root.name
    )
    blueprint_id = sanitize_blueprint_id(raw_id)
    blueprint = {
        "id": blueprint_id,
        "name": manifest.get("job_name") or identity.get("name") or blueprint_id,
        "path": bundle_root.name,
        "description": manifest.get("description") or "",
        "category": DEFAULT_CATEGORY,
        "category_slug": category_slug(DEFAULT_CATEGORY),
        "source": "local_path",
        "local_path": str(bundle_root),
    }
    return bundle_root.parent, blueprint


def run_mn_blueprint_validate(bundle_root: Path, *, timeout_seconds: int = 120) -> Dict[str, Any]:
    bundle_root = bundle_root.expanduser().resolve()
    if not bundle_root.is_dir():
        return validation_failure_report(f"blueprint path is not a directory: {bundle_root}")

    command = mn_validate_command(bundle_root)
    env = subprocess_environment()
    env.update(runtime_path_environment())
    try:
        result = subprocess.run(
            command,
            cwd=bundle_root,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return validation_failure_report("mn CLI was not found. Install mn or run the API from the monorepo environment.")
    except subprocess.TimeoutExpired:
        return validation_failure_report(f"mn blueprint validate timed out after {timeout_seconds}s.")

    output = clean_validation_output(result.stdout)
    error_output = clean_validation_output(result.stderr)
    report = parse_validation_json(output)
    if report is None:
        combined = "\n".join(part for part in [output, error_output] if part).strip()
        if result.returncode == 0:
            report = {
                "version": 1,
                "schema_version": "validation.report/v1",
                "ok": True,
                "status": "passed",
                "errors": [],
                "issues": [],
                "results": [],
                "message": combined or "mn blueprint validate passed",
            }
        else:
            report = validation_failure_report(combined or "mn blueprint validate failed")
    else:
        report.setdefault("ok", result.returncode == 0 and report.get("ok") is not False)
        report.setdefault("status", "passed" if report.get("ok") else "failed")
        report.setdefault("errors", [])
        report.setdefault("issues", [])
        report.setdefault("results", [])
        if result.returncode != 0:
            report["ok"] = False
            report["status"] = "failed"

    report["command"] = " ".join(command)
    report["exit_code"] = result.returncode
    if error_output:
        report["stderr"] = error_output[-4000:]
    return report


def mn_base_command() -> list[str]:
    mn_binary = shutil.which("mn")
    if mn_binary:
        return [mn_binary]
    return [sys.executable, "-m", "mn_cli.main"]


def mn_validate_command(bundle_root: Path) -> list[str]:
    return mn_base_command() + ["blueprint", "validate", str(bundle_root), "--output", "json"]


def clean_validation_output(value: str | None) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", value or "").strip()


def parse_validation_json(output: str) -> Dict[str, Any] | None:
    if not output:
        return None
    try:
        decoded = json.loads(output)
        return decoded if isinstance(decoded, dict) else None
    except json.JSONDecodeError:
        pass

    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        decoded = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def validation_failure_report(message: str) -> Dict[str, Any]:
    message = message.strip() or "mn blueprint validate failed"
    return {
        "version": 1,
        "schema_version": "validation.report/v1",
        "ok": False,
        "status": "failed",
        "error_count": 1,
        "errors": [message],
        "issues": [
            {
                "code": "blueprint_validation_failed",
                "message": message,
                "help": "Fix the blueprint folder and try again.",
                "severity": "error",
            }
        ],
        "results": [],
    }


def load_blueprint_bundle(
    repo_root: Path,
    blueprint: Dict[str, Any],
    run_id: str,
    *,
    config_overrides: Dict[str, Any] | None = None,
    env_overrides: Dict[str, str] | None = None,
    force: bool = False,
    progress_callback: Callable[[str, str, str], None] | None = None,
    stable_job_id: str | None = None,
    submission_id: str | None = None,
) -> tuple[str, Dict[str, bytes]]:
    from mn_api import state

    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    manifest_path = bundle_root / "manifest.json"

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint manifest.json is malformed") from exc

    if not isinstance(manifest, dict):
        raise HTTPException(status_code=500, detail="blueprint manifest.json must be an object")
    runs_root = shared_runs_root()
    preparation_env = string_env_values(env_overrides)
    preparation_env.setdefault("MN_RUN_ID", run_id)
    preparation_env["MN_RUNS_ROOT"] = runs_root
    submission_metadata = {
        "blueprint_id": blueprint["id"],
        "blueprint_run_id": run_id,
        "blueprint_source": str(repo_root),
    }
    if blueprint.get("revision"):
        submission_metadata["blueprint_revision"] = blueprint["revision"]

    shared_preparation = prepare_manifest_submission(
        bundle_root,
        manifest,
        env_overrides=preparation_env,
        submission_metadata=submission_metadata,
        config_overrides=config_overrides,
        runtime_environment=runtime_path_environment(),
        read_json_object_fn=read_json_object,
    )
    manifest = shared_preparation.manifest
    runtime_env = shared_preparation.runtime_environment
    package_payload_models_for_api(bundle_root, manifest)
    prepare_openshell_custom_images(bundle_root, manifest)

    metadata = manifest.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        manifest["metadata"] = metadata
    metadata["blueprint_id"] = blueprint["id"]
    metadata["blueprint_run_id"] = run_id
    metadata["run_id"] = run_id
    if force:
        metadata["mn_validation"] = {
            "force": True,
            "status": "skipped",
            "skipped_checks": ["input_validation", "soft_requirements"],
        }
    if blueprint.get("revision"):
        metadata["blueprint_revision"] = blueprint["revision"]
    manifest["run_id"] = run_id

    placement = None
    placement_env = {**os.environ, **string_env_values(env_overrides)}
    placement_mode = workflow_placement_mode(manifest, env=placement_env)
    if placement_mode == "single_node" or (
        placement_mode is None and workflow_requires_single_node(manifest)
    ):
        try:
            resource_report = json.loads(state.client.get_resource())
            system_summary = json.loads(state.client.get_system_summary())
        except Exception as exc:
            raise RuntimeError(
                f"could not inspect runtime nodes for workflow placement: {exc}"
            ) from exc
        placement = resolve_and_apply_workflow_placement(
            manifest,
            resource_report=resource_report if isinstance(resource_report, dict) else {},
            system_summary=system_summary if isinstance(system_summary, dict) else {},
            env=placement_env,
            constraint_source="mn-api-workflow-placement",
        )
    if placement:
        selected_node = str(placement["selected_node"])
        runtime_env["MN_SELECTED_RUNTIME_NODE"] = selected_node
        inject_node_environment(
            manifest, {"MN_SELECTED_RUNTIME_NODE": selected_node}
        )
        if progress_callback:
            progress_callback(
                "Workflow placement resolved.",
                f"All workflow nodes are pinned to {selected_node}.",
                "Node-local workers and generated control nodes will run together.",
            )

    hostlocal_nodes = hostlocal_python_environment_nodes(manifest)
    if hostlocal_nodes:
        if progress_callback:
            progress_callback(
                "Preparing HostLocal Python environments.",
                "Building or reusing isolated Python environments required by local workflow services.",
                "A first launch may take several minutes while Python packages are installed.",
            )
        prepare_hostlocal_python_environments_for_submission(bundle_root, manifest)

    payloads = stage_payload_assets(
        manifest,
        bundle_root,
        blob_root=resolve_mn_home() / "blobs",
    )
    stage_blueprint_payloads_for_submission(manifest, payloads, bundle_dir=bundle_root)
    if progress_callback and any(
        str((node.get("config") or {}).get("runner_module") or "") == "MirrorNeuron.Runner.DockerWorker"
        for node in manifest_agent_nodes(manifest)
        if isinstance(node.get("config"), dict)
    ):
        progress_callback(
            "Preparing DockerWorker runtime.",
            "Building or reusing the DockerWorker image and starting its shared container.",
            "The first launch can take several minutes while runtime dependencies are installed. Keep Docker running.",
        )
    with temporary_process_environment(runtime_process_environment()):
        prepared = prepare_job_submission(
            manifest,
            payloads,
            bundle_dir=bundle_root,
            run_id=run_id,
            job_id=stable_job_id,
            submission_id=submission_id,
            cluster_client=state.client,
            env={**os.environ, **runtime_env},
        )

    return prepared.manifest_json, prepared.payloads


def hostlocal_python_environment_nodes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for node in manifest_agent_nodes(manifest):
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if config.get("runner_module") != "MirrorNeuron.Runner.HostLocal":
            continue
        python_environment = (
            config.get("python_environment")
            if isinstance(config.get("python_environment"), dict)
            else {}
        )
        packages = [
            str(package).strip()
            for package in python_environment.get("packages") or []
            if isinstance(package, str) and package.strip()
        ]
        requirements = str(python_environment.get("requirements") or "").strip()
        if packages or requirements:
            nodes.append(node)
    return nodes


def prepare_hostlocal_python_environments_for_submission(
    bundle_root: Path,
    manifest: dict[str, Any],
    *,
    runtime_client: Any | None = None,
    timeout: float | None = None,
) -> list[dict[str, str]]:
    from mn_api import state

    blueprint_id = hostlocal_blueprint_id(bundle_root, manifest)
    resolved_timeout = timeout or config_float(
        "MN_BLUEPRINT_PYTHON_ENV_TIMEOUT_SECONDS",
        default=30.0,
    )
    prepared: list[dict[str, str]] = []
    for node in hostlocal_python_environment_nodes(manifest):
        config = node["config"]
        python_environment = config["python_environment"]
        node_id = str(node.get("node_id") or node.get("id") or "host_local")
        packages = [
            str(package).strip()
            for package in python_environment.get("packages") or []
            if isinstance(package, str) and package.strip()
        ]
        requirements = str(python_environment.get("requirements") or "").strip()
        requirements_content = hostlocal_requirements_content(
            bundle_root,
            node_id=node_id,
            requirements=requirements,
        )
        selected_node = hostlocal_selected_runtime_node(manifest, node)
        response = call_prepare_runtime_model(
            runtime_client or hostlocal_runtime_client(selected_node),
            {
                "node": selected_node,
                "ensure_hostlocal_python_environment": True,
                "blueprint_id": blueprint_id,
                "node_id": node_id,
                "packages": packages,
                "requirements_content": requirements_content,
                "timeout": resolved_timeout,
                "source": "mn-api",
            },
            logger=state.logger,
        )
        runtime_path = str(response.get("runtime_path") or "").strip()
        if not runtime_path:
            raise RuntimeError(
                f"{node_id}: HostLocal Python environment preparation did not return a runtime path"
            )
        python_environment["path"] = runtime_path
        prepared.append(
            {
                "node_id": node_id,
                "path": runtime_path,
                "host_path": str(response.get("host_path") or ""),
            }
        )
    return prepared


def hostlocal_requirements_content(
    bundle_root: Path,
    *,
    node_id: str,
    requirements: str,
) -> str:
    if not requirements:
        return ""
    payload_root = (bundle_root / "payloads").resolve()
    requirement_file = (payload_root / requirements).resolve()
    try:
        requirement_file.relative_to(payload_root)
    except ValueError as exc:
        raise RuntimeError(
            f"{node_id}: python_environment.requirements must be relative inside payloads/"
        ) from exc
    if not requirement_file.is_file():
        raise RuntimeError(
            f"{node_id}: python_environment requirements file not found: payloads/{requirements}"
        )
    return requirement_file.read_text(encoding="utf-8")


def hostlocal_blueprint_id(bundle_root: Path, manifest: dict[str, Any]) -> str:
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    mn_cli = metadata.get("mn_cli") if isinstance(metadata.get("mn_cli"), dict) else {}
    return str(
        metadata.get("blueprint_id")
        or mn_cli.get("blueprint_id")
        or bundle_root.name
        or "blueprint"
    )


def hostlocal_selected_runtime_node(
    manifest: dict[str, Any],
    node: dict[str, Any],
) -> str:
    policies = node.get("policies") if isinstance(node.get("policies"), dict) else {}
    scheduler = policies.get("scheduler") if isinstance(policies.get("scheduler"), dict) else {}
    explicit = str(
        scheduler.get("preferred_node")
        or scheduler.get("preferredNode")
        or ""
    ).strip()
    if explicit:
        return explicit
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    placement = (
        metadata.get("mn_workflow_placement")
        if isinstance(metadata.get("mn_workflow_placement"), dict)
        else {}
    )
    return str(placement.get("selected_node") or "").strip()


def hostlocal_runtime_client(selected_node: str):
    from mn_api import state

    if not selected_node:
        return state.client
    try:
        summary = json.loads(state.client.get_system_summary())
    except Exception as exc:
        raise RuntimeError(
            f"could not inspect runtime nodes for HostLocal environment preparation: {exc}"
        ) from exc
    nodes = summary.get("nodes") if isinstance(summary, dict) else []
    selected = next(
        (
            node
            for node in nodes or []
            if isinstance(node, dict)
            and str(node.get("name") or node.get("node") or "").strip() == selected_node
        ),
        None,
    )
    if not selected:
        raise RuntimeError(
            f"runtime node {selected_node} was not found for HostLocal environment preparation"
        )
    if selected.get("self?") is True or selected.get("self") is True:
        return state.client

    candidates = [selected.get("native_sdk_grpc")]
    for key in ("hardware", "node_info"):
        nested = selected.get(key) if isinstance(selected.get(key), dict) else {}
        candidates.append(nested.get("native_sdk_grpc"))
    native = next((candidate for candidate in candidates if isinstance(candidate, dict) and candidate), None)
    if not native or native.get("enabled") is False:
        raise RuntimeError(
            f"runtime node {selected_node} does not advertise an enabled native SDK gRPC endpoint"
        )
    target = str(native.get("target") or "").strip()
    host = str(native.get("host") or "").strip()
    port = str(native.get("port") or "").strip()
    if not target and host and port:
        target = f"{host}:{port}"
    if not target:
        raise RuntimeError(
            f"runtime node {selected_node} advertises incomplete native SDK gRPC metadata"
        )
    current_config = state.refresh_config_from_env()
    return Client(
        target=target,
        timeout=runtime_model_prepare_timeout_seconds(),
        auth_token=getattr(current_config, "grpc_auth_token", None),
        admin_token=getattr(current_config, "grpc_admin_token", None),
    )


def prepare_skill_runtime_for_submission(
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    bundle_dir: Path,
) -> dict[str, Any] | None:
    return sdk_prepare_skill_runtime_for_manifest(
        manifest,
        config,
        bundle_dir=bundle_dir,
        workspace_root=workspace_root(),
    )


def ensure_blueprint_support_sdk_build_context_uploads_for_submission(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    return ensure_blueprint_support_sdk_build_context_uploads(manifest)


def refresh_embedded_blueprint_config_for_submission(
    manifest: dict[str, Any],
    config: dict[str, Any],
) -> None:
    refresh_embedded_blueprint_config(manifest, config)


def localize_skill_dependencies_for_submission(manifest: dict[str, Any]) -> dict[str, Any]:
    return localize_skill_dependencies_for_dev(manifest)


def localize_agent_dependencies_for_submission(manifest: dict[str, Any]) -> dict[str, Any]:
    return localize_agent_dependencies_for_dev(manifest)


def inject_skill_dependency_python_environments_for_submission(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    return inject_skill_dependency_python_environments(manifest)


def skill_dependency_package_names_for_submission(manifest: dict[str, Any]) -> set[str]:
    return skill_dependency_package_names(manifest)


def release_skill_dependency_runtime_environment_for_submission(
    env: dict[str, str],
) -> dict[str, str]:
    return release_skill_dependency_runtime_environment(env)


def stage_blueprint_payloads_for_submission(
    manifest: dict[str, Any],
    payloads: dict[str, bytes],
    *,
    bundle_dir: Path,
) -> None:
    stage_upload_path_payloads_for_manifest(manifest, payloads, bundle_dir=bundle_dir)
    stage_blueprint_support_payloads_for_manifest(manifest, payloads, bundle_dir=bundle_dir)
    sdk_stage_skill_runtime_payloads_for_manifest(manifest, payloads, bundle_dir=bundle_dir)
    stage_skill_dependency_payloads_for_manifest(manifest, payloads, bundle_dir=bundle_dir)


def strip_docker_model_runner_placement_requirements_for_submission(
    manifest: dict[str, Any],
) -> None:
    strip_docker_model_runner_placement_requirements(manifest)


def normalize_host_local_uploads_for_submission(manifest: dict[str, Any]) -> None:
    normalize_host_local_uploads(manifest)


def lower_manifest_topology_for_submission(manifest: dict[str, Any]) -> None:
    lower_manifest_topology_for_runtime_submission(manifest)


def stage_local_input_payloads_for_manifest(
    manifest: Dict[str, Any],
    payloads: Dict[str, bytes],
    *,
    bundle_dir: Path,
) -> Dict[str, Any]:
    ensure_runtime_modules_for_submission(manifest)
    try:
        return stage_sdk_local_input_payloads(manifest, payloads, bundle_dir=bundle_dir)
    except RuntimeError as exc:
        raise AppError(
            "MN_INVALID_ARGUMENT",
            "Local input payloads could not be prepared for this blueprint.",
            internal_message=str(exc),
            hint="Review the local input configuration and try again.",
            exit_code=2,
            http_status=400,
            cause=exc,
        ) from exc


def manifest_agent_nodes(manifest: Dict[str, Any]) -> list[Dict[str, Any]]:
    return sdk_submission_manifest_nodes(manifest)


def prepare_openshell_custom_images(bundle_root: Path, manifest: Dict[str, Any]) -> None:
    nodes = manifest_agent_nodes(manifest)
    if not nodes:
        return

    for node in nodes:
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        if config.get("runner_module") != "MirrorNeuron.Sandbox.OpenShell":
            continue

        custom_image = config.get("custom_openshell_image")
        if custom_image is not None:
            source_path = openshell_local_from_path(bundle_root, custom_image)
            if source_path is None:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"custom_openshell_image for {node.get('node_id') or 'OpenShell node'} "
                        f"must point to a payload directory or Dockerfile: {custom_image}"
                    ),
                )
        else:
            source_path = openshell_local_from_path(bundle_root, config.get("from"))

        if source_path is None:
            continue

        config["from"] = build_openshell_sandbox_image(source_path)


def openshell_local_from_path(bundle_root: Path, source: Any) -> Path | None:
    if not isinstance(source, str) or not source.strip() or "://" in source:
        return None

    source_value = source.strip()
    raw = Path(source_value).expanduser()
    candidates = [raw] if raw.is_absolute() else [bundle_root / "payloads" / source_value, bundle_root / source_value]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir() and (resolved / "Dockerfile").is_file():
            return resolved
        if resolved.is_file() and resolved.name == "Dockerfile":
            return resolved
    return None


def openshell_config_dir() -> Path:
    return config_path("OPENSHELL_CONFIG_DIR", default=Path.home() / ".config" / "openshell") or (
        Path.home() / ".config" / "openshell"
    )


def openshell_gateway_name() -> str:
    configured = config_value("OPENSHELL_GATEWAY").strip()
    if configured:
        return configured
    config_dir = openshell_config_dir()
    try:
        active = (config_dir / "active_gateway").read_text(encoding="utf-8").strip()
        if active:
            return active
    except OSError:
        pass
    if (config_dir / "gateways" / "openshell" / "metadata.json").is_file():
        return "openshell"
    return ""


def openshell_gateway_metadata(gateway_name: str) -> Dict[str, Any]:
    if not gateway_name:
        return {}
    try:
        metadata = json.loads((openshell_config_dir() / "gateways" / gateway_name / "metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def openshell_gateway_uses_local_docker() -> bool:
    gateway_name = openshell_gateway_name()
    if not gateway_name:
        return False
    metadata = openshell_gateway_metadata(gateway_name)
    if metadata.get("is_remote") is True:
        return False
    endpoint = metadata.get("gateway_endpoint")
    if not isinstance(endpoint, str):
        return False
    parsed = urlparse(endpoint)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def openshell_env() -> Dict[str, str]:
    env = subprocess_environment()
    if env.get("OPENSHELL_GATEWAY_ENDPOINT"):
        return env
    gateway_name = openshell_gateway_name()
    if gateway_name:
        env.setdefault("OPENSHELL_GATEWAY", gateway_name)
    return env


def build_openshell_sandbox_image(source_path: Path) -> str:
    source_path = source_path.resolve()
    if openshell_gateway_uses_local_docker():
        digest = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:12]
        image_ref = f"openshell/sandbox-from:{digest}"
        result = subprocess.run(
            ["docker", "build", "-t", image_ref, str(source_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".strip() or "docker build failed"
            raise HTTPException(status_code=500, detail=f"OpenShell sandbox image build failed: {detail}")
        return image_ref

    result = subprocess.run(
        [
            "openshell",
            "sandbox",
            "create",
            "--from",
            str(source_path),
            "--no-tty",
            "--no-keep",
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        env=openshell_env(),
    )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"OpenShell sandbox image build failed: {output.strip()}")
    matches = re.findall(r"Image\s+([^\s]+)\s+is available in the gateway", output)
    if not matches:
        raise HTTPException(status_code=500, detail="OpenShell did not report an image reference")
    return ANSI_ESCAPE_PATTERN.sub("", matches[-1])


def render_agent_templates_for_submission(
    manifest: Dict[str, Any], *, bundle_root: Path | None = None
) -> None:
    nodes = manifest_agent_nodes(manifest)
    if not nodes or not any(isinstance(node, dict) and "uses" in node for node in nodes):
        return
    ensure_runtime_modules_for_submission(manifest)
    local_root = payload_agent_root(bundle_root) if bundle_root is not None else None
    rendered = render_manifest_agent_templates(manifest, agent_root=local_root)
    manifest.clear()
    manifest.update(rendered)


def start_blueprint_pre_launch_hook(
    repo_root: Path,
    blueprint: Dict[str, Any],
    run_id: str,
    *,
    config_overrides: Dict[str, Any] | None = None,
    env_overrides: Dict[str, str] | None = None,
) -> subprocess.Popen[Any] | None:
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    register_post_launch_hook(bundle_root, run_id)
    script_path = (bundle_root / PRE_LAUNCH_SCRIPT).resolve()
    if not script_path.is_file():
        return None

    runs_root = Path(shared_runs_root()).expanduser()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ready_file = run_dir / "pre_launch.ready"
    try:
        ready_file.unlink()
    except FileNotFoundError:
        pass

    config = with_shared_run_store_config(
        load_blueprint_config(bundle_root, config_overrides=config_overrides),
        run_id,
        str(runs_root),
    )
    runtime_env = blueprint_runtime_environment(
        bundle_root,
        config=config,
        config_overrides=config_overrides,
    )
    env = subprocess_environment()
    env.update(runtime_env)
    env.update(string_env_values(env_overrides))
    env.update(
        {
            "MN_RUN_ID": run_id,
            "MN_RUN_DIR": str(run_dir),
            "MN_RUNS_ROOT": str(runs_root),
            "MN_BLUEPRINT_BUNDLE_DIR": str(bundle_root),
            "MN_BLUEPRINT_CONFIG_JSON": json.dumps(config, sort_keys=True),
            "MN_PRE_LAUNCH_READY_FILE": str(ready_file),
            "MN_POST_LAUNCH_STATE_FILE": str(run_dir / "post_launch_state.json"),
        }
    )

    command = ["bash", str(script_path)]
    log_path = run_dir / "pre_launch.log"
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=bundle_root,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
    except OSError as exc:
        raise AppError(
            "MN_EXECUTION_FAILED",
            "The blueprint pre-launch hook could not be started.",
            internal_message=str(exc),
            hint="Check the API logs and the blueprint pre-launch script permissions.",
            http_status=500,
            cause=exc,
        ) from exc

    process_info = {
        "pid": process.pid,
        "command": command,
        "script": str(script_path),
        "log": str(log_path),
        "ready_file": str(ready_file),
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    process_group_id = process_group_id_for_pid(process.pid)
    if process_group_id:
        process_info["process_group_id"] = process_group_id
    session_id = process_session_id_for_pid(process.pid)
    if session_id:
        process_info["session_id"] = session_id
    (run_dir / "pre_launch_process.json").write_text(json.dumps(process_info, indent=2, sort_keys=True) + "\n")

    try:
        wait_for_pre_launch_ready(run_dir, process, ready_file)
        apply_pre_launch_ready_metadata(
            ready_file,
            config_overrides=config_overrides,
            env_overrides=env_overrides,
        )
    except Exception as exc:
        terminate_pre_launch_process(process)
        cleanup_run_process(run_dir, "pre_launch_process.json")
        cleanup_post_launch_hook(run_dir, reason="pre_launch_failed")
        raise AppError(
            "MN_EXECUTION_FAILED",
            "The blueprint pre-launch hook did not become ready.",
            internal_message=str(exc),
            hint="Check the API logs and the blueprint pre-launch log.",
            http_status=500,
            cause=exc,
        ) from exc
    return process


def register_post_launch_hook(bundle_root: Path, run_id: str) -> None:
    script_path = (bundle_root / POST_LAUNCH_SCRIPT).resolve()
    if not script_path.is_file():
        return
    runs_root = Path(shared_runs_root()).expanduser()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    hook_info = {
        "command": ["bash", str(script_path)],
        "script": str(script_path),
        "cwd": str(bundle_root),
        "log": str(run_dir / "post_launch.log"),
        "run_id": run_id,
        "bundle_dir": str(bundle_root),
        "state_file": str(run_dir / "post_launch_state.json"),
        "pre_launch_ready_file": str(run_dir / "pre_launch.ready"),
        "pre_launch_process_file": str(run_dir / "pre_launch_process.json"),
        "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (run_dir / "post_launch_hook.json").write_text(json.dumps(hook_info, indent=2, sort_keys=True) + "\n")


def wait_for_pre_launch_ready(run_dir: Path, process: subprocess.Popen[Any], ready_file: Path) -> None:
    try:
        timeout = max(config_float("MN_PRE_LAUNCH_TIMEOUT_SECONDS", default=30.0), 0)
    except ValueError:
        timeout = 30.0
    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        if ready_file.exists():
            return
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            raise RuntimeError(f"Blueprint pre-launch hook exited before becoming ready. See {run_dir / 'pre_launch.log'}.")
        time.sleep(0.1)
    raise RuntimeError(f"Blueprint pre-launch hook timed out after {timeout:g}s. See {run_dir / 'pre_launch.log'}.")


def apply_pre_launch_ready_metadata(
    ready_file: Path,
    *,
    config_overrides: Dict[str, Any] | None,
    env_overrides: Dict[str, str] | None = None,
) -> None:
    raw = ready_file.read_text().strip()
    if not raw or raw == "ready":
        return
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(metadata, dict):
        return
    env_patch = metadata.get("env")
    if isinstance(env_patch, dict) and env_overrides is not None:
        env_overrides.update(string_env_values(env_patch))
    config_patch = metadata.get("config") or metadata.get("config_overrides")
    if isinstance(config_patch, dict) and config_overrides is not None:
        merged = deep_merge(config_overrides, config_patch)
        config_overrides.clear()
        config_overrides.update(merged)


def terminate_pre_launch_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None:
        return
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
    else:
        try:
            process.terminate()
        except OSError:
            pass


def positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 1 else None


def process_group_id_for_pid(pid: int) -> int | None:
    try:
        return positive_int(os.getpgid(pid))
    except OSError:
        return None


def process_session_id_for_pid(pid: int) -> int | None:
    try:
        return positive_int(os.getsid(pid))
    except OSError:
        return None


def cleanup_timeout_seconds() -> float:
    try:
        return max(config_float("MN_PROCESS_CLEANUP_TIMEOUT_SECONDS", default=PROCESS_CLEANUP_TIMEOUT_SECONDS), 0.1)
    except ValueError:
        return PROCESS_CLEANUP_TIMEOUT_SECONDS


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except OSError:
        return False


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_until_gone(exists, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not exists():
            return True
        time.sleep(0.1)
    return not exists()


def reap_child_pid(pid: int) -> bool:
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return False
    return waited_pid == pid


def wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reap_child_pid(pid) or not pid_exists(pid):
            return True
        time.sleep(0.1)
    return reap_child_pid(pid) or not pid_exists(pid)


def terminate_process_group(process_group_id: int | None, *, leader_pid: int | None = None) -> None:
    if not process_group_id:
        return
    current_group = process_group_id_for_pid(os.getpid())
    if current_group == process_group_id:
        return
    if not process_group_exists(process_group_id):
        return

    timeout = cleanup_timeout_seconds()

    def group_is_gone() -> bool:
        if leader_pid is not None:
            reap_child_pid(leader_pid)
        return not process_group_exists(process_group_id)

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except OSError:
        return
    if wait_until_gone(lambda: not group_is_gone(), timeout):
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        pass
    wait_until_gone(lambda: not group_is_gone(), timeout)


def terminate_pid(pid: int | None) -> None:
    if not pid or not pid_exists(pid):
        return
    timeout = cleanup_timeout_seconds()
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if wait_for_pid_exit(pid, timeout):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    wait_for_pid_exit(pid, timeout)


def cleanup_run_process(run_dir: Path, metadata_name: str) -> None:
    process_path = run_dir / metadata_name
    if not process_path.is_file():
        return
    try:
        data = json.loads(process_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    pid = positive_int(data.get("pid"))
    process_group_id = positive_int(data.get("process_group_id") or data.get("pgid"))
    if process_group_id is None and pid is not None:
        process_group_id = process_group_id_for_pid(pid)
    terminate_process_group(process_group_id, leader_pid=pid)
    terminate_pid(pid)


def read_optional_json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def collect_run_cleanup_metadata(run_dir: Path) -> Dict[str, Any]:
    process_info = read_optional_json_object(run_dir / "pre_launch_process.json")
    state = read_optional_json_object(run_dir / "post_launch_state.json")
    ready = read_optional_json_object(run_dir / "pre_launch.ready")
    ready_env = ready.get("env") if isinstance(ready.get("env"), dict) else {}
    ports = {
        positive_int(state.get("rtsp_port")),
        positive_int(state.get("webrtc_port")),
        positive_int(state.get("webrtc_local_tcp_port")),
        positive_int(ready_env.get("RTSP_PORT")),
        positive_int(ready_env.get("WEBRTC_PORT")),
        positive_int(ready_env.get("WEBRTC_LOCAL_TCP_PORT")),
    }
    pids = {
        positive_int(process_info.get("pid")),
        positive_int(state.get("server_pid")),
        positive_int(state.get("publisher_pid")),
    }
    process_group_ids = {
        positive_int(process_info.get("process_group_id") or process_info.get("pgid")),
    }
    return {
        "ports": {port for port in ports if port is not None},
        "pids": {pid for pid in pids if pid is not None},
        "process_group_ids": {pgid for pgid in process_group_ids if pgid is not None},
    }


def command_for_pid(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def listener_pids_for_port(port: int) -> set[int]:
    if not shutil.which("lsof"):
        return set()
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return set()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        pid = positive_int(line.strip())
        if pid is not None:
            pids.add(pid)
    return pids


def cleanup_owned_port_listeners(metadata: Dict[str, Any]) -> None:
    ports = metadata.get("ports") if isinstance(metadata.get("ports"), set) else set()
    if not ports:
        return
    recorded_pids = metadata.get("pids") if isinstance(metadata.get("pids"), set) else set()
    recorded_groups = metadata.get("process_group_ids") if isinstance(metadata.get("process_group_ids"), set) else set()
    for port in ports:
        for pid in listener_pids_for_port(port):
            if port_listener_is_cleanup_owned(pid, recorded_pids, recorded_groups):
                terminate_pid(pid)


def port_listener_is_cleanup_owned(pid: int, recorded_pids: set[int], recorded_groups: set[int]) -> bool:
    if pid in recorded_pids:
        return True
    process_group_id = process_group_id_for_pid(pid)
    if process_group_id is not None and process_group_id in recorded_groups:
        return True
    command = command_for_pid(pid)
    owns_video_watch_artifacts = (
        "video_watch_assistant" in command
        or "/tmp/video_watch_assistant_mediamtx." in command
    )
    if not owns_video_watch_artifacts:
        return False
    return any(token in command for token in ("mediamtx", "rtsp-simple-server", "ffmpeg", "pre-launch.sh"))


def cleanup_post_launch_hook(run_dir: Path, *, reason: str) -> None:
    hook_path = run_dir / "post_launch_hook.json"
    if not hook_path.is_file():
        return
    try:
        hook_info = json.loads(hook_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(hook_info, dict):
        return

    script_value = hook_info.get("script")
    if not isinstance(script_value, str) or not script_value:
        return
    script_path = Path(script_value)
    if not script_path.is_file():
        return

    command = hook_info.get("command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        command = ["bash", str(script_path)]

    cwd_value = hook_info.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else script_path.parent
    if not cwd.is_dir():
        cwd = script_path.parent

    log_value = hook_info.get("log")
    log_path = Path(log_value) if isinstance(log_value, str) and log_value else run_dir / "post_launch.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    env = subprocess_environment()
    env.update(post_launch_env(run_dir, hook_info, reason=reason))
    try:
        timeout = max(config_float("MN_POST_LAUNCH_TIMEOUT_SECONDS", default=10.0), 0.1)
    except ValueError:
        timeout = 10.0
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- post-launch cleanup reason={reason} ---\n")
            subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                text=True,
            )
    except (OSError, subprocess.TimeoutExpired):
        return


def post_launch_env(run_dir: Path, hook_info: Dict[str, Any], *, reason: str) -> Dict[str, str]:
    ready_value = hook_info.get("pre_launch_ready_file")
    ready_path = Path(ready_value) if isinstance(ready_value, str) and ready_value else run_dir / "pre_launch.ready"
    pre_launch_process_value = hook_info.get("pre_launch_process_file")
    state_value = hook_info.get("state_file")
    pre_launch_process_path = Path(pre_launch_process_value) if isinstance(pre_launch_process_value, str) and pre_launch_process_value else run_dir / "pre_launch_process.json"
    env = {
        "MN_RUN_ID": str(hook_info.get("run_id") or run_dir.name),
        "MN_RUN_DIR": str(run_dir),
        "MN_RUNS_ROOT": str(run_dir.parent),
        "MN_BLUEPRINT_BUNDLE_DIR": str(hook_info.get("bundle_dir") or ""),
        "MN_PRE_LAUNCH_READY_FILE": str(ready_path),
        "MN_PRE_LAUNCH_PROCESS_FILE": str(pre_launch_process_path),
        "MN_POST_LAUNCH_STATE_FILE": str(state_value or run_dir / "post_launch_state.json"),
        "MN_POST_LAUNCH_REASON": reason,
    }
    try:
        pre_launch_process = json.loads(pre_launch_process_path.read_text())
    except (OSError, json.JSONDecodeError):
        pre_launch_process = {}
    if isinstance(pre_launch_process, dict):
        pid = positive_int(pre_launch_process.get("pid"))
        process_group_id = positive_int(pre_launch_process.get("process_group_id") or pre_launch_process.get("pgid"))
        if pid:
            env["MN_PRE_LAUNCH_PID"] = str(pid)
        if process_group_id:
            env["MN_PRE_LAUNCH_PROCESS_GROUP_ID"] = str(process_group_id)
    try:
        ready = json.loads(ready_path.read_text())
    except (OSError, json.JSONDecodeError):
        ready = {}
    ready_env = ready.get("env") if isinstance(ready, dict) else None
    if isinstance(ready_env, dict):
        env.update({str(key): str(value) for key, value in ready_env.items() if value is not None})
    return env


def cleanup_blueprint_run_processes(run_id: str, *, reason: str = "job_cancelled") -> None:
    run_dir = Path(shared_runs_root()).expanduser() / run_id
    cleanup_metadata = collect_run_cleanup_metadata(run_dir)
    cleanup_post_launch_hook(run_dir, reason=reason)
    cleanup_run_process(run_dir, "pre_launch_process.json")
    cleanup_owned_port_listeners(cleanup_metadata)
    cleanup_run_process(run_dir, "web_ui_process.json")
    cleanup_run_process(run_dir, "event_relay.json")


def start_background_event_relay_if_needed(
    repo_root: Path,
    blueprint: Dict[str, Any],
    run_id: str,
    job_id: str,
    manifest_json: str,
    *,
    config_overrides: Dict[str, Any] | None = None,
    env_overrides: Dict[str, str] | None = None,
    grpc_target: str | None = None,
    grpc_auth_token: str | None = None,
    grpc_timeout_seconds: float | None = None,
) -> None:
    if not config_bool("MN_RUN_BACKGROUND_EVENT_RELAY", default=True):
        return
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError:
        return
    runs_root = Path(shared_runs_root()).expanduser()
    run_dir = runs_root / run_id
    has_post_launch_hook = (run_dir / "post_launch_hook.json").is_file()
    if not has_post_launch_hook:
        return

    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    config = with_shared_run_store_config(
        load_blueprint_config(bundle_root, config_overrides=config_overrides),
        run_id,
        str(runs_root),
    )
    max_seconds = background_event_relay_max_seconds(config)
    poll_seconds = background_event_relay_poll_seconds(config)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "event_relay.log"
    command = [
        sys.executable,
        "-m",
        "mn_sdk.blueprint_support.event_relay",
        "--job-id",
        job_id,
        "--run-dir",
        str(run_dir),
        "--poll-seconds",
        f"{poll_seconds:g}",
    ]
    if max_seconds is not None:
        command.extend(["--max-seconds", f"{max_seconds:g}"])

    env = subprocess_environment()
    env.update(runtime_path_environment())
    env.update(string_env_values(env_overrides))
    env["MN_RUN_EVENT_RELAY_CHILD"] = "1"
    if grpc_target:
        env["MN_GRPC_TARGET"] = grpc_target
    if grpc_auth_token:
        env["MN_GRPC_AUTH_TOKEN"] = grpc_auth_token
    if grpc_timeout_seconds is None:
        env.setdefault("MN_GRPC_TIMEOUT_SECONDS", "10")
    else:
        env["MN_GRPC_TIMEOUT_SECONDS"] = f"{grpc_timeout_seconds:g}"

    try:
        with log_path.open("a", encoding="utf-8") as relay_log:
            process = subprocess.Popen(
                command,
                stdout=relay_log,
                stderr=relay_log,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=env,
            )
    except OSError:
        return

    relay_info = {
        "job_id": job_id,
        "pid": process.pid,
        "poll_seconds": poll_seconds,
        "max_seconds": max_seconds,
        "log_path": str(log_path),
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    process_group_id = process_group_id_for_pid(process.pid)
    if process_group_id:
        relay_info["process_group_id"] = process_group_id
    (run_dir / "event_relay.json").write_text(json.dumps(relay_info, indent=2, sort_keys=True) + "\n")


def background_event_relay_poll_seconds(config: Dict[str, Any] | None) -> float:
    raw = config_optional_value("MN_RUN_EVENT_RELAY_POLL_SECONDS")
    if raw is not None:
        try:
            return max(float(raw), 0.1)
        except ValueError:
            return 1.0

    return 1.0


def background_event_relay_max_seconds(config: Dict[str, Any] | None) -> float | None:
    raw = config_optional_value("MN_RUN_EVENT_RELAY_MAX_SECONDS")
    if raw is not None:
        if raw.strip().lower() in {"", "0", "none", "infinity"}:
            return None
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return None

    config = config if isinstance(config, dict) else {}
    budgets = config.get("budgets") if isinstance(config.get("budgets"), dict) else {}
    try:
        return max(float(budgets.get("max_stream_duration_seconds", 3600)), 0.0)
    except (TypeError, ValueError):
        return 3600.0


def active_job_ids_from_jobs_payload(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        jobs = payload.get("data") or payload.get("jobs") or []
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []

    active_job_ids: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").lower()
        if status in TERMINAL_JOB_STATUSES:
            continue
        job_id = job.get("job_id") or job.get("id")
        if isinstance(job_id, str) and job_id:
            active_job_ids.add(job_id)
    return active_job_ids


def scheduler_allocated_ports_from_jobs_payload(
    payload: Any,
    *,
    active_job_ids: set[str] | None = None,
) -> set[int]:
    ports: set[int] = set()
    for job in job_dicts_from_payload(payload):
        if active_job_ids is not None:
            job_id = job_id_from_payload(job)
            if job_id is not None and job_id not in active_job_ids:
                continue
        ports.update(scheduler_allocated_ports_from_job(job))
    return ports


def job_dicts_from_payload(payload: Any) -> list[Dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "jobs"):
            jobs = payload.get(key)
            if isinstance(jobs, list):
                return [job for job in jobs if isinstance(job, dict)]
        return [payload]
    if isinstance(payload, list):
        return [job for job in payload if isinstance(job, dict)]
    return []


def job_id_from_payload(payload: Dict[str, Any]) -> str | None:
    for candidate in (
        payload.get("job_id"),
        payload.get("id"),
        as_dict(payload.get("job")).get("job_id"),
        as_dict(payload.get("job")).get("id"),
        as_dict(payload.get("summary")).get("job_id"),
        as_dict(payload.get("summary")).get("id"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def scheduler_allocated_ports_from_job(job: Dict[str, Any]) -> set[int]:
    ports: set[int] = set()
    for candidate in (
        job,
        as_dict(job.get("job")),
        as_dict(job.get("summary")),
    ):
        scheduler = as_dict(candidate.get("scheduler"))
        placements = scheduler.get("placements")
        if not isinstance(placements, list):
            continue
        for placement in placements:
            if not isinstance(placement, dict):
                continue
            allocations = as_dict(placement.get("allocations"))
            allocated_ports = allocations.get("ports")
            if not isinstance(allocated_ports, list):
                continue
            for allocated_port in allocated_ports:
                raw_port = allocated_port.get("port") if isinstance(allocated_port, dict) else allocated_port
                try:
                    port = int(raw_port)
                except (TypeError, ValueError):
                    continue
                if 1 <= port <= 65535:
                    ports.add(port)
    return ports


def cleanup_stale_blueprint_run_processes(
    repo_root: Path,
    blueprint: Dict[str, Any],
    *,
    keep_run_id: str,
    active_job_ids: set[str] | None,
    reason: str = "stale_blueprint_run",
) -> None:
    runs_root = Path(shared_runs_root()).expanduser()
    if not runs_root.is_dir():
        return

    bundle_root = validate_blueprint_bundle(repo_root, blueprint).resolve()
    blueprint_id = str(blueprint.get("id") or "")
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == keep_run_id:
            continue
        if not run_dir_matches_blueprint(run_dir, blueprint_id=blueprint_id, bundle_root=bundle_root):
            continue

        job_id = run_dir_job_id(run_dir)
        if job_id:
            if active_job_ids is None or job_id in active_job_ids:
                continue
        elif not unmapped_run_dir_is_stale(run_dir):
            continue

        cleanup_blueprint_run_processes(run_dir.name, reason=reason)


def run_dir_matches_blueprint(run_dir: Path, *, blueprint_id: str, bundle_root: Path) -> bool:
    job_path = run_dir / "job.json"
    try:
        job_data = json.loads(job_path.read_text())
    except (OSError, json.JSONDecodeError):
        job_data = {}
    if isinstance(job_data, dict):
        recorded_blueprint_id = job_data.get("blueprint_id")
        if isinstance(recorded_blueprint_id, str) and recorded_blueprint_id:
            return recorded_blueprint_id == blueprint_id

    hook_path = run_dir / "post_launch_hook.json"
    try:
        hook_data = json.loads(hook_path.read_text())
    except (OSError, json.JSONDecodeError):
        hook_data = {}
    if not isinstance(hook_data, dict):
        return False

    bundle_value = hook_data.get("bundle_dir")
    if not isinstance(bundle_value, str) or not bundle_value:
        return False
    try:
        return Path(bundle_value).expanduser().resolve() == bundle_root
    except OSError:
        return False


def run_dir_job_id(run_dir: Path) -> str:
    try:
        data = json.loads((run_dir / "job.json").read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    job_id = data.get("job_id") or data.get("id")
    return job_id if isinstance(job_id, str) else ""


def unmapped_run_dir_is_stale(run_dir: Path) -> bool:
    try:
        return time.time() - run_dir.stat().st_mtime >= UNMAPPED_RUN_STALE_SECONDS
    except OSError:
        return False


def cleanup_blueprint_processes_for_job(job_id: str) -> None:
    runs_root = Path(shared_runs_root()).expanduser()
    if not runs_root.is_dir():
        return
    for run_dir in runs_root.iterdir():
        job_path = run_dir / "job.json"
        if not job_path.is_file():
            continue
        try:
            data = json.loads(job_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("job_id") == job_id:
            cleanup_blueprint_run_processes(run_dir.name)
            return


def validate_blueprint_inputs(
    repo_root: Path,
    blueprint: Dict[str, Any],
    *,
    config_overrides: Dict[str, Any] | None = None,
    env_overrides: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    manifest_path = bundle_root / "manifest.json"

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=500, detail="blueprint manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_root, manifest)
    resolve_blueprint_payload_contract(manifest, bundle_root)

    spec_issues = (
        validate_service_spec_issues(manifest)
        + validate_requirements_spec_issues(manifest)
        + validate_resource_spec_issues(manifest)
        + validate_input_validation_spec_issues(manifest)
    )
    if spec_issues:
        return make_validation_report(spec_issues)

    hardware_result = run_hardware_requirements_validation(
        manifest,
        resource_report=runtime_resource_report,
    )
    if not hardware_result.get("ok"):
        return hardware_result

    config = load_blueprint_config(bundle_root, config_overrides=config_overrides)
    env = blueprint_runtime_environment(
        bundle_root,
        config=config,
        config_overrides=config_overrides,
    )
    env.update(string_env_values(env_overrides))
    service_result = run_service_validation(
        bundle_root,
        manifest,
        config=config,
        env=env,
        resolver=_runtime_service_resolver(),
    )
    if not service_result.get("ok"):
        return service_result

    validation_manifest, validation_config = model_validation_inputs_with_prepared_models(
        manifest,
        config,
        prepared_runtime_model_keys_from_env(env),
    )
    model_result = run_model_validation(
        bundle_root,
        validation_manifest,
        config=validation_config,
        env=env,
        installed_resolver=prepared_model_installed_resolver(env),
    )
    if not model_result.get("ok"):
        return model_result

    return run_input_validation(bundle_root, manifest, config=config, env=env)


def validate_blueprint_hardware_requirements(
    repo_root: Path,
    blueprint: Dict[str, Any],
    *,
    force: bool = False,
) -> Dict[str, Any]:
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    try:
        manifest = json.loads((bundle_root / "manifest.json").read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=500, detail="blueprint manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_root, manifest)
    return run_hardware_requirements_validation(
        manifest,
        resource_report=runtime_resource_report,
        force=force,
    )


def runtime_resource_report() -> dict[str, Any]:
    try:
        from mn_api import state

        decoded = json.loads(state.client.get_resource())
    except Exception:
        return {"nodes": []}
    return decoded if isinstance(decoded, dict) else {"nodes": []}


def _runtime_service_resolver():
    def resolver(name: str, requirement: dict[str, Any]) -> list[dict[str, Any]]:
        from mn_api import state

        response = state.client.resolve_service(
            name,
            tags=requirement.get("tags") or [],
            passing_only=True,
        )
        decoded = json.loads(response)
        services = decoded.get("services") if isinstance(decoded, dict) else []
        return services if isinstance(services, list) else []

    return resolver


def shared_runs_root() -> str:
    return sdk_shared_runs_root()


def with_shared_run_store_config(
    config: Optional[Dict[str, Any]],
    run_id: str,
    runs_root: str,
) -> Dict[str, Any]:
    return sdk_with_shared_run_store_config(config, run_id, runs_root)


def write_blueprint_job_mapping(
    blueprint_run_id: str,
    job_id: str,
    run_id: str,
    *,
    blueprint_id: str | None = None,
    blueprint_revision: str | None = None,
    blueprint_source: str | None = None,
    blueprint_path: str | None = None,
    monitor_manifest: Dict[str, Any] | None = None,
) -> Path:
    return sdk_write_blueprint_job_mapping(
        blueprint_run_id,
        job_id,
        run_id,
        root=shared_runs_root(),
        blueprint_id=blueprint_id,
        blueprint_revision=blueprint_revision,
        blueprint_source=blueprint_source,
        blueprint_path=blueprint_path,
        monitor_manifest=monitor_manifest,
    )


def enrich_blueprint_from_manifest(repo_root: Path, blueprint: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return sdk_enrich_blueprint_from_manifest(repo_root, blueprint)
    except BlueprintCatalogError as exc:
        raise _catalog_http_exception(exc) from exc


def load_optional_manifest(repo_root: Path, blueprint: Dict[str, Any]) -> Dict[str, Any]:
    return sdk_load_optional_manifest(repo_root, blueprint)


def manifest_init_config_review(manifest: Dict[str, Any]) -> Any:
    return sdk_manifest_init_config_review(manifest)


def blueprint_runtime_environment(
    bundle_root: Path,
    *,
    config: Dict[str, Any] | None = None,
    config_overrides: Dict[str, Any] | None = None,
) -> Dict[str, str]:
    return sdk_blueprint_runtime_environment(
        bundle_root,
        config=config,
        config_overrides=config_overrides,
        runtime_env=runtime_path_environment(),
        read_json_object_fn=read_json_object,
    )


def apply_manifest_config_bindings(manifest: Dict[str, Any], config: Dict[str, Any]) -> None:
    sdk_apply_manifest_config_bindings(manifest, config)


def config_to_environment(config: Dict[str, Any]) -> Dict[str, str]:
    return sdk_config_to_environment(config)


def set_manifest_path(target: Any, dotted_path: str, value: Any) -> None:
    sdk_set_manifest_path(target, dotted_path, value)


def config_path_get(config: Dict[str, Any], dotted_path: str) -> Any:
    return sdk_config_path_get(config, dotted_path)


def load_blueprint_config(
    bundle_root: Path,
    *,
    config_overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    return sdk_load_blueprint_config(
        bundle_root,
        config_overrides=config_overrides,
        read_json_object_fn=read_json_object,
    )


def load_blueprint_config_overwrites(
    bundle_root: Path,
    *,
    config_overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    return sdk_load_blueprint_config_overwrites(
        bundle_root,
        config_overrides=config_overrides,
        read_json_object_fn=read_json_object,
    )


def inject_node_environment(manifest: Dict[str, Any], env: Dict[str, str]) -> None:
    sdk_inject_node_environment(
        manifest,
        env,
        nodes=manifest_agent_nodes(manifest),
        skip_host_local_dmr_rewrite=False,
    )


def merge_path_values(*values: str) -> str:
    return sdk_merge_path_values(*values)


def add_mn_llm_aliases(environment: Dict[str, Any]) -> None:
    sdk_add_mn_llm_aliases(environment)


def adjust_llm_environment_for_node(environment: Dict[str, Any], node: Dict[str, Any]) -> None:
    sdk_adjust_llm_environment_for_node(environment, node, skip_host_local=False)


def read_json_object(path: Path) -> Dict[str, Any]:
    try:
        decoded = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"{path.name} is malformed") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=500, detail=f"{path.name} must contain a JSON object")
    return decoded


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    return sdk_deep_merge(base, override)
