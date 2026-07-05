from __future__ import annotations

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
    DOCKER_MODEL_RUNNER_CONTAINER_API_BASE,
    cluster_provided_model,
    docker_api_model_name,
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
    prepare_job_submission,
    record_model_owner,
    required_blueprint_models,
    resolve_llm_environment,
    resolve_model_endpoint,
    resolve_model_entry,
    run_hardware_requirements_validation,
    run_input_validation,
    run_model_validation,
    run_service_validation,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
    validate_resource_spec_issues,
    validate_service_spec_issues,
)
from mn_sdk.blueprint_source import is_git_repo_url
from mn_sdk.context_engine import blueprint_requires_context_engine
from mn_sdk.runtime_modules import (
    RuntimeModuleInstallError,
    default_registered_modules_root,
    ensure_runtime_modules_for_manifest,
)
from mn_sdk.blueprint_support import (
    inject_runtime_web_ui_service,
    render_manifest_agent_templates,
    runtime_web_ui_service_from_manifest as sdk_runtime_web_ui_service_from_manifest,
    runtime_web_ui_support_payloads,
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
from mn_api.path_utils import default_runs_root, inside_path


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


def workspace_root() -> Path:
    for name in ("MN_WORKSPACE_ROOT",):
        value = config_value(name)
        if value:
            return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def runtime_path_environment() -> Dict[str, str]:
    root = workspace_root()
    runtime_modules_root = default_registered_modules_root(workspace_root=root)
    runtime_env = runtime_env_values()
    membrane_project_path = Path(config_value("MN_MEMBRANE_PROJECT_PATH") or root / "Membrane").expanduser()
    membrane_sdk_path = Path(
        config_value("MN_MEMBRANE_SDK_PATH")
        or membrane_project_path / "mn-context-engine-python-sdk" / "src"
    ).expanduser()
    skills_root = Path(config_value("MN_SKILLS_ROOT", runtime_env=runtime_env) or runtime_modules_root).expanduser()
    env = {
        "MN_WORKSPACE_ROOT": str(root),
        "MN_MEMBRANE_PROJECT_PATH": str(membrane_project_path),
        "MN_MEMBRANE_SDK_PATH": str(membrane_sdk_path),
        "MN_SKILLS_ROOT": str(skills_root),
    }
    python_paths = [
        skills_root / "llm_ocr_skill" / "src",
        skills_root / "pdf_extract_skill" / "src",
    ]
    existing_pythonpath = config_value("PYTHONPATH")
    resolved_python_paths = [str(path) for path in python_paths if path.exists()]
    if existing_pythonpath:
        resolved_python_paths.append(existing_pythonpath)
    if resolved_python_paths:
        env["PYTHONPATH"] = os.pathsep.join(resolved_python_paths)
    env["PATH"] = docker_cli_path_environment().get("PATH", config_value("PATH"))
    return env


def ensure_runtime_modules_for_submission(
    manifest: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    try:
        return ensure_runtime_modules_for_manifest(manifest, config, workspace_root=workspace_root())
    except RuntimeModuleInstallError as exc:
        raise AppError(
            "MN_EXECUTION_FAILED",
            "A required runtime module could not be installed automatically.",
            internal_message=str(exc),
            hint="Check the API logs and runtime module configuration, then try again.",
            http_status=500,
            cause=exc,
        ) from exc


def blueprint_web_ui_enabled(config: Dict[str, Any] | None) -> bool:
    web_ui = config.get("web_ui") if isinstance(config, dict) else None
    return isinstance(web_ui, dict) and web_ui.get("enabled") is True


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
    if not isinstance(value, str):
        return DEFAULT_CATEGORY
    category = value.strip()
    return category or DEFAULT_CATEGORY


def category_slug(value: Any) -> str:
    category = normalize_category_name(value)
    slug = CATEGORY_SLUG_PATTERN.sub("-", category.lower()).strip("-")
    return slug or CATEGORY_SLUG_PATTERN.sub("-", DEFAULT_CATEGORY.lower()).strip("-")


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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{blueprint_id}-{stamp}"


def cached_git_repo_path(repo_url: str) -> Path:
    parsed = urlparse(repo_url)
    name = Path(parsed.path.rstrip("/")).stem or "blueprints"
    digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
    configured_cache = (
        config_path("MN_BLUEPRINT_REPO_CACHE", default=DEFAULT_BLUEPRINT_REPO_CACHE)
        or Path(DEFAULT_BLUEPRINT_REPO_CACHE).expanduser()
    )
    return configured_cache / f"{name}-{digest}"


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def clone_git_blueprint_repo(repo_url: str, target: Path) -> None:
    temp_target = target.with_name(f".{target.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    if temp_target.exists():
        shutil.rmtree(temp_target)
    run_git(["clone", "--depth", "1", repo_url, str(temp_target)])
    if target.exists():
        shutil.rmtree(target)
    temp_target.replace(target)


def ensure_git_blueprint_repo(repo_url: str) -> Path:
    target = cached_git_repo_path(repo_url)
    if (target / "index.json").is_file() and not (target / ".git").is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not (target / ".git").is_dir():
        raise HTTPException(status_code=500, detail="blueprint repo cache path exists but is not a git repository")
    try:
        if target.exists():
            status = run_git(["-C", str(target), "status", "--porcelain"])
            if status.stdout.strip():
                run_git(["-C", str(target), "reset", "--hard", "HEAD"])
                run_git(["-C", str(target), "clean", "-fdx"])
            try:
                run_git(["-C", str(target), "pull", "--ff-only"])
            except (OSError, subprocess.CalledProcessError):
                clone_git_blueprint_repo(repo_url, target)
        else:
            clone_git_blueprint_repo(repo_url, target)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise HTTPException(status_code=500, detail=f"blueprint repo clone failed: {detail}") from exc
    return target


def blueprint_repo_root(config: ApiConfig) -> Path:
    source = str(getattr(config, "blueprint_source", "") or "").strip().lower()
    if source == "github":
        repo_value = getattr(config, "blueprint_repo", "")
        if not repo_value:
            raise HTTPException(status_code=500, detail="MN_BLUEPRINT_REPO is not configured")
        if not is_git_repo_url(repo_value):
            raise HTTPException(status_code=500, detail="MN_BLUEPRINT_REPO must be a Git URL")
        return ensure_git_blueprint_repo(repo_value)

    if source != "local":
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_SOURCE must be github or local")

    local_value = getattr(config, "blueprint_local", "") or getattr(config, "active_blueprint_location", "")
    if not local_value:
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_LOCAL is not configured")
    repo_root = Path(local_value).expanduser().resolve()
    if not repo_root.is_dir():
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_LOCAL is not a directory")
    if not (repo_root / "index.json").is_file():
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_LOCAL index.json was not found")
    return repo_root


def load_blueprint_catalog(config: ApiConfig) -> tuple[Path, list[Dict[str, Any]]]:
    repo_root = blueprint_repo_root(config)
    index_path = repo_root / "index.json"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="blueprint repo index.json was not found")

    try:
        index_data = json.loads(index_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint repo index.json is malformed") from exc

    entries = index_data.get("blueprints") if isinstance(index_data, dict) else index_data
    if not isinstance(entries, list):
        raise HTTPException(status_code=500, detail="blueprint repo index.json must be a list")

    blueprints: list[Dict[str, Any]] = []
    for entry in entries:
        normalized = normalize_blueprint(entry)
        if normalized:
            blueprints.append(enrich_blueprint_from_manifest(repo_root, normalized))
    return repo_root, blueprints


def normalize_blueprint(entry: Any) -> Optional[Dict[str, Any]]:
    record = as_dict(entry)
    product = as_dict(record.get("product"))
    pricing = as_dict(record.get("pricing"))
    blueprint_id = record.get("id") or record.get("blueprint_id") or record.get("blueprintId")
    if not isinstance(blueprint_id, str) or not blueprint_id:
        return None

    pricing_model = pricing.get("model") or record.get("pricing_model") or record.get("pricingModel") or "free"
    pricing_rate = pricing.get("rate") or record.get("hourly_rate") or record.get("hourlyRate") or 0
    pricing_unit = pricing.get("unit") or "hour"
    try:
        pricing_rate = float(pricing_rate)
    except (TypeError, ValueError):
        pricing_rate = 0

    runtime_features = (
        record.get("runtime_features")
        or record.get("runtimeFeatures")
        or product.get("runtime_features")
        or product.get("runtimeFeatures")
        or []
    )
    capabilities = record.get("capabilities") or product.get("capabilities") or []
    category = normalize_category_name(record.get("category") or product.get("category"))
    rate_label = (
        record.get("rate_label")
        or record.get("rateLabel")
        or ("Free" if pricing_model == "free" or not pricing_rate else f"${pricing_rate:g}/{pricing_unit}")
    )
    hourly_rate = (
        record.get("hourly_rate")
        or record.get("hourlyRate")
        or ("$0/hr" if pricing_model == "free" or not pricing_rate else f"${pricing_rate:g}/{pricing_unit}")
    )

    return {
        "id": blueprint_id,
        "type": record.get("type") or product.get("type") or "batch",
        "name": record.get("name") or product.get("name") or blueprint_id,
        "tagline": record.get("tagline") or product.get("tagline") or product.get("one_line") or "",
        "summary": record.get("summary") or product.get("summary") or product.get("one_line") or "",
        "description": record.get("description") or product.get("description") or product.get("one_line") or "",
        "job_name": record.get("job_name") or record.get("jobName") or product.get("job_name") or "",
        "workflow_id": record.get("workflow_id") or record.get("workflowId") or product.get("workflow_id") or "",
        "target_users": record.get("target_users") or record.get("targetUsers") or product.get("target_users") or "",
        "output": record.get("output") or product.get("output") or "",
        "agent_role": record.get("agent_role") or record.get("agentRole") or product.get("agent_role") or "",
        "customizable_for": (
            record.get("customizable_for")
            or record.get("customizableFor")
            or product.get("customizable_for")
            or ""
        ),
        "problem": record.get("problem") or product.get("problem") or "",
        "simulation_type": (
            record.get("simulation_type")
            or record.get("simulationType")
            or product.get("simulation_type")
            or ""
        ),
        "category": category,
        "category_slug": category_slug(category),
        "publisher": record.get("publisher") or product.get("publisher") or "MirrorNeuron",
        "rating": record.get("rating") or product.get("rating") or 0,
        "installs": record.get("installs") or product.get("installs") or 0,
        "pricing": {
            "model": pricing_model,
            "rate": pricing_rate,
            "unit": pricing_unit,
        },
        "requirements": record.get("requirements") if isinstance(record.get("requirements"), dict) else {},
        "rate_label": rate_label,
        "hourly_rate": hourly_rate,
        "capabilities": as_list(capabilities),
        "runtime_features": as_list(runtime_features),
        "icon": record.get("icon") or product.get("icon") or "",
        "accent_color": record.get("accent_color") or record.get("accentColor") or "#1f7a8c",
        "revision": record.get("revision") or record.get("blueprint_revision") or "",
        "path": record.get("path") or record.get("directory") or blueprint_id,
    }


def load_blueprint_categories(repo_root: Path, blueprints: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    category_records: list[Dict[str, str]] = []
    category_path = repo_root / "category.json"
    if category_path.is_file():
        try:
            category_data = json.loads(category_path.read_text())
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail="blueprint repo category.json is malformed") from exc

        categories = as_dict(category_data).get("categories")
        if not isinstance(categories, list):
            raise HTTPException(status_code=500, detail="blueprint repo category.json must contain a categories list")
        for position, entry in enumerate(categories):
            record = as_dict(entry)
            raw_name = record.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise HTTPException(status_code=500, detail=f"blueprint category entry {position} is malformed")
            name = raw_name.strip()
            slug = record.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                slug = category_slug(name)
            else:
                slug = category_slug(slug)
            category_records.append({"name": name, "slug": slug})

    counts: dict[str, int] = {}
    names_by_slug: dict[str, str] = {}
    for blueprint in blueprints:
        name = normalize_category_name(blueprint.get("category"))
        slug = category_slug(blueprint.get("category_slug") or name)
        counts[slug] = counts.get(slug, 0) + 1
        names_by_slug.setdefault(slug, name)

    categories_by_slug: dict[str, Dict[str, Any]] = {}
    ordered_slugs: list[str] = []
    for record in category_records:
        slug = record["slug"]
        if slug not in categories_by_slug:
            ordered_slugs.append(slug)
        categories_by_slug[slug] = {
            "name": record["name"],
            "slug": slug,
            "count": counts.get(slug, 0),
        }

    for slug in sorted(counts):
        if slug in categories_by_slug:
            continue
        ordered_slugs.append(slug)
        categories_by_slug[slug] = {
            "name": names_by_slug.get(slug, slug.replace("-", " ").title()),
            "slug": slug,
            "count": counts[slug],
        }

    return [categories_by_slug[slug] for slug in ordered_slugs]


def filter_blueprints_by_category(
    blueprints: list[Dict[str, Any]],
    category: str | None,
) -> list[Dict[str, Any]]:
    if category is None or not category.strip():
        return blueprints

    requested = {
        category_slug(part)
        for part in category.split(",")
        if part.strip()
    }
    if not requested:
        return blueprints
    return [
        blueprint
        for blueprint in blueprints
        if category_slug(blueprint.get("category_slug") or blueprint.get("category")) in requested
    ]


def find_blueprint(config: ApiConfig, blueprint_id: str) -> tuple[Path, Dict[str, Any]]:
    validate_blueprint_id(blueprint_id)
    repo_root, blueprints = load_blueprint_catalog(config)
    for blueprint in blueprints:
        if blueprint["id"] == blueprint_id:
            return repo_root, blueprint
    raise HTTPException(status_code=404, detail="blueprint not found")


def blueprint_bundle_root(repo_root: Path, blueprint: Dict[str, Any]) -> Path:
    blueprint_path = Path(str(blueprint.get("path") or blueprint["id"]))
    if blueprint_path.is_absolute():
        candidate = blueprint_path.resolve()
    else:
        candidate = (repo_root / blueprint_path).resolve()

    if not inside_path(candidate, repo_root):
        raise HTTPException(status_code=400, detail="blueprint path escapes repository")
    return candidate


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
    config = load_blueprint_config(bundle_root, config_overrides=config_overrides)
    catalog = load_model_catalog()
    requirements = required_blueprint_models(manifest, config, catalog=catalog)
    ledger = load_model_ownership()
    results: list[dict[str, Any]] = []
    service_results: list[dict[str, Any]] = []
    errors: list[str] = []
    prepared_docker_models: dict[str, dict[str, Any]] = {}
    endpoints: dict[str, dict[str, Any]] = {}
    blueprint_id = str(blueprint.get("id") or "")
    blueprint_revision = str(blueprint.get("revision") or "")
    for requirement in requirements:
        model_ref = str(requirement.get("model") or "")
        try:
            entry = resolve_model_entry(model_ref, catalog=catalog)
        except KeyError:
            message = f"{requirement.get('path')}: unknown runtime model {model_ref!r}"
            results.append({"model": model_ref, "status": "failed", "error": message})
            errors.append(message)
            continue
        provider = str(entry.get("provider") or "docker_model_runner")
        target = docker_model_name(entry)
        backend = str(requirement.get("backend") or entry.get("backend") or "auto")
        base_result = {
            "id": entry.get("id"),
            "model": target,
            "provider": provider,
            "backend": backend,
            "path": requirement.get("path"),
        }
        if cluster_provided_model(requirement):
            results.append({**base_result, "status": "cluster_provided"})
            continue
        if provider != "docker_model_runner":
            record_model_owner(
                entry,
                blueprint_id=blueprint_id,
                blueprint_revision=blueprint_revision,
                install_source=str(repo_root),
                backend=backend,
            )
            results.append({**base_result, "status": "service_required"})
            continue
        endpoint = resolve_runtime_model_endpoint_for_api(requirement=requirement, entry=entry)
        if endpoint:
            keys = {
                str(requirement.get("name") or "").strip(),
                str(requirement.get("model") or "").strip(),
                str(entry.get("id") or "").strip(),
                target,
                str(endpoint.get("model") or "").strip(),
                str(endpoint.get("runtime_model") or "").strip(),
            }
            keys.update(str(alias or "").strip() for alias in entry.get("aliases") or [])
            for key in keys:
                if key:
                    endpoints[key] = endpoint
            results.append({**base_result, "status": endpoint.get("source") or "cluster_provided", "endpoint": endpoint})
            continue
        previous_result = prepared_docker_models.get(target)
        if previous_result is not None:
            duplicate_result = {
                **base_result,
                "status": previous_result.get("status", "already_installed"),
                "duplicate_of": previous_result.get("path"),
            }
            if previous_result.get("error"):
                duplicate_result["error"] = previous_result["error"]
            results.append(duplicate_result)
            continue
        preexisting_record = ledger.get("models", {}).get(target)
        try:
            installed = docker_model_installed(target)
        except Exception:
            installed = False
        if not installed:
            command = mn_base_command() + ["model", "install", str(entry.get("id") or model_ref)]
            if backend and backend != "auto":
                command.extend(["--backend", backend])
            if requirement.get("context_size"):
                command.extend(["--context-size", str(requirement["context_size"])])
            if force:
                command.append("--force")
            result = subprocess.run(command, cwd=str(bundle_root), capture_output=True, text=True, timeout=1200, check=False)
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "model install failed").strip()
                failed_result = {**base_result, "status": "failed", "error": message}
                prepared_docker_models[target] = failed_result
                results.append(failed_result)
                errors.append(message)
                continue
        record_model_owner(
            entry,
            blueprint_id=blueprint_id,
            blueprint_revision=blueprint_revision,
            install_source=str(repo_root),
            backend=backend,
            preexisting_manual=installed and not isinstance(preexisting_record, dict),
        )
        ready_result = {**base_result, "status": "already_installed" if installed else "installed"}
        prepared_docker_models[target] = ready_result
        results.append(ready_result)
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
    env = {}
    if endpoints:
        env["MN_MODEL_ENDPOINTS_JSON"] = model_endpoints_json(endpoints)
    prepared_json = prepared_runtime_models_json(results)
    if prepared_json:
        env["MN_PREPARED_RUNTIME_MODELS_JSON"] = prepared_json
    return {"ok": not errors, "models": results, "services": service_results, "endpoints": endpoints, "env": env, "errors": errors}


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
        return None


def prepared_runtime_models_json(results: list[dict[str, Any]]) -> str:
    keys = prepared_runtime_model_keys({"models": results})
    return json.dumps(sorted(keys), separators=(",", ":")) if keys else ""


def prepared_runtime_model_keys(model_install_summary: dict[str, Any] | None) -> set[str]:
    prepared_statuses = {
        "installed",
        "already_installed",
        "runtime_node_install",
        "runtime_node_already_installed",
        "runtime_node_installed",
        "cluster_provided",
        "service_registry",
        "model_remote",
        "explicit_config",
    }
    keys: set[str] = set()
    models = model_install_summary.get("models") if isinstance(model_install_summary, dict) else []
    for item in models or []:
        if not isinstance(item, dict) or str(item.get("status") or "") not in prepared_statuses:
            continue
        for key in ("id", "model", "runtime_model", "name"):
            value = str(item.get(key) or "").strip()
            if value:
                keys.add(value)
        endpoint = item.get("endpoint") if isinstance(item.get("endpoint"), dict) else {}
        for key in ("model", "runtime_model"):
            value = str(endpoint.get(key) or "").strip()
            if value:
                keys.add(value)
    return keys


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
    if not prepared:
        return manifest, config
    manifest_copy = json.loads(json.dumps(manifest))
    config_copy = json.loads(json.dumps(config))
    runtime = manifest_copy.get("runtime") if isinstance(manifest_copy.get("runtime"), dict) else {}
    models = runtime.get("models") if isinstance(runtime.get("models"), dict) else {}
    for entry in models.values():
        if isinstance(entry, dict) and model_config_matches_prepared(entry, prepared):
            entry["install_mode"] = "cluster_provided"
    llm = config_copy.get("llm") if isinstance(config_copy.get("llm"), dict) else {}
    configs = llm.get("configs") if isinstance(llm.get("configs"), dict) else {}
    for entry in configs.values():
        if isinstance(entry, dict) and model_config_matches_prepared(entry, prepared):
            entry["install_mode"] = "cluster_provided"
    if isinstance(llm, dict) and model_config_matches_prepared(llm, prepared):
        llm["install_mode"] = "cluster_provided"
    return manifest_copy, config_copy


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
    command = mn_base_command() + ["runtime", "ensure-context-engine"]
    if force:
        command.append("--force")
    result = subprocess.run(command, cwd=str(bundle_root), capture_output=True, text=True, timeout=1200, check=False)
    base_result: dict[str, Any] = {
        "name": "membrane-context-engine",
        "status": "ready" if result.returncode == 0 else "failed",
        "command": " ".join(command),
    }
    if result.returncode != 0:
        base_result["error"] = (result.stderr or result.stdout or "context engine setup failed").strip()
    return base_result


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


def run_mn_blueprint_run(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int = 300,
    env_overrides: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    command = mn_base_command() + ["blueprint", "run", *args]
    env = subprocess_environment()
    env.update(runtime_path_environment())
    env.update(string_env_values(env_overrides))
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "exit_code": 127,
            "command": " ".join(command),
            "error": "mn CLI was not found. Install mn or run the API from the monorepo environment.",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": 124,
            "command": " ".join(command),
            "error": f"mn blueprint run timed out after {timeout_seconds}s.",
        }

    stdout = clean_validation_output(result.stdout)
    stderr = clean_validation_output(result.stderr)
    job_id = parse_cli_field(stdout, "Job ID") or parse_json_field(stdout, "job_id") or parse_json_field(stdout, "id")
    run_id = parse_cli_field(stdout, "Blueprint Run ID") or parse_json_field(stdout, "run_id")
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "command": " ".join(command),
        "job_id": job_id,
        "run_id": run_id,
        "stdout": stdout[-8000:],
        "stderr": stderr[-8000:],
        "error": stderr or stdout or "mn blueprint run failed",
    }


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


def parse_json_field(output: str, field: str) -> str | None:
    decoded = parse_validation_json(output)
    if not decoded:
        return None
    value = decoded.get(field)
    return str(value) if value else None


def parse_cli_field(output: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s+([A-Za-z0-9_.:-]+)", output)
    return match.group(1) if match else None


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
    web_ui_reserved_ports: set[int] | None = None,
) -> tuple[str, Dict[str, bytes]]:
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    manifest_path = bundle_root / "manifest.json"
    payloads_path = bundle_root / "payloads"

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="blueprint manifest.json is malformed") from exc

    if not isinstance(manifest, dict):
        raise HTTPException(status_code=500, detail="blueprint manifest.json must be an object")
    manifest = expand_blueprint_manifest_if_source(bundle_root, manifest)

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
    ensure_runtime_modules_for_submission(manifest)
    render_agent_templates_for_submission(manifest)
    materialize_agent_topology_for_runtime(manifest)
    prepare_openshell_custom_images(bundle_root, manifest)
    runs_root = shared_runs_root()
    config = with_shared_run_store_config(
        load_blueprint_config(bundle_root, config_overrides=config_overrides),
        run_id,
        runs_root,
    )
    if config is not None:
        apply_manifest_config_bindings(manifest, config)
        ensure_runtime_modules_for_submission(manifest, config)
        if blueprint_web_ui_enabled(config):
            inject_runtime_web_ui_service_for_submission(
                manifest,
                bundle_dir=bundle_root,
                config=config,
                run_id=run_id,
                runs_root=runs_root,
                env_overrides=env_overrides,
                reserved_ports=web_ui_reserved_ports,
            )
    runtime_env = blueprint_runtime_environment(
        bundle_root,
        config=config,
        config_overrides=config_overrides,
    )
    runtime_env.update(string_env_values(env_overrides))
    runtime_env.setdefault("MN_RUN_ID", run_id)
    runtime_env["MN_RUNS_ROOT"] = runs_root
    expand_manifest_model_service_requirements(manifest, config or {}, env=runtime_env)
    if runtime_env:
        inject_node_environment(manifest, runtime_env)

    payloads: Dict[str, bytes] = {}
    if payloads_path.is_dir():
        for payload_path in payloads_path.rglob("*"):
            if payload_path.is_file():
                payloads[payload_path.relative_to(payloads_path).as_posix()] = payload_path.read_bytes()
    payloads.update(runtime_web_ui_support_payloads_for_manifest(manifest))
    prepared = prepare_job_submission(
        manifest,
        payloads,
        bundle_dir=bundle_root,
        run_id=run_id,
    )

    return prepared.manifest_json, prepared.payloads


def inject_runtime_web_ui_service_for_submission(
    manifest: Dict[str, Any],
    *,
    bundle_dir: Path,
    config: Dict[str, Any],
    run_id: str,
    runs_root: str,
    env_overrides: Dict[str, str] | None = None,
    reserved_ports: set[int] | None = None,
) -> Dict[str, Any] | None:
    ensure_runtime_modules_for_submission(manifest, config)
    try:
        return inject_runtime_web_ui_service(
            manifest,
            bundle_dir=bundle_dir,
            config=config,
            run_id=run_id,
            runs_root=runs_root,
            env_overrides=env_overrides,
            reserved_ports=reserved_ports,
        )
    except RuntimeError as exc:
        raise AppError(
            "MN_FAILED_PRECONDITION",
            "The runtime web UI service could not be prepared for this blueprint.",
            internal_message=str(exc),
            hint="Review the blueprint web UI configuration and try again.",
            http_status=409,
            cause=exc,
        ) from exc


def runtime_web_ui_support_payloads_for_manifest(manifest: Dict[str, Any]) -> Dict[str, bytes]:
    ensure_runtime_modules_for_submission(manifest)
    if not sdk_runtime_web_ui_service_from_manifest(manifest):
        return {}
    return runtime_web_ui_support_payloads()


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
    agents = manifest.get("agents") if isinstance(manifest.get("agents"), dict) else {}
    agent_nodes = agents.get("nodes") if isinstance(agents.get("nodes"), list) else None
    if isinstance(agent_nodes, list):
        return [node for node in agent_nodes if isinstance(node, dict)]
    root_nodes = manifest.get("nodes")
    if isinstance(root_nodes, list):
        return [node for node in root_nodes if isinstance(node, dict)]
    return []


def materialize_agent_topology_for_runtime(manifest: Dict[str, Any]) -> None:
    if isinstance(manifest.get("nodes"), list):
        return
    agents = manifest.get("agents") if isinstance(manifest.get("agents"), dict) else {}
    agent_nodes = agents.get("nodes") if isinstance(agents.get("nodes"), list) else None
    if not isinstance(agent_nodes, list) or not agent_nodes:
        return
    manifest["nodes"] = agent_nodes
    agent_edges = agents.get("edges") if isinstance(agents.get("edges"), list) else []
    manifest["edges"] = agent_edges
    agent_entrypoints = agents.get("entrypoints") if isinstance(agents.get("entrypoints"), list) else []
    manifest["entrypoints"] = agent_entrypoints


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


def render_agent_templates_for_submission(manifest: Dict[str, Any]) -> None:
    nodes = manifest_agent_nodes(manifest)
    if not nodes or not any(isinstance(node, dict) and "uses" in node for node in nodes):
        return
    ensure_runtime_modules_for_submission(manifest)
    rendered = render_manifest_agent_templates(manifest)
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
    service_info = runtime_web_ui_service_from_manifest(manifest)
    has_post_launch_hook = (run_dir / "post_launch_hook.json").is_file()
    if not service_info and not has_post_launch_hook:
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
        "service": service_info,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    process_group_id = process_group_id_for_pid(process.pid)
    if process_group_id:
        relay_info["process_group_id"] = process_group_id
    (run_dir / "event_relay.json").write_text(json.dumps(relay_info, indent=2, sort_keys=True) + "\n")


def runtime_web_ui_service_from_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    service_info = metadata.get("blueprint_web_ui_service") if isinstance(metadata, dict) else {}
    return service_info if isinstance(service_info, dict) else {}


def background_event_relay_poll_seconds(config: Dict[str, Any] | None) -> float:
    raw = config_optional_value("MN_RUN_EVENT_RELAY_POLL_SECONDS")
    if raw is not None:
        try:
            return max(float(raw), 0.1)
        except ValueError:
            return 1.0

    config = config if isinstance(config, dict) else {}
    web_ui = config.get("web_ui") if isinstance(config.get("web_ui"), dict) else {}
    output = web_ui.get("output") if isinstance(web_ui.get("output"), dict) else {}
    try:
        return max(float(output.get("refresh_seconds", 1.0)), 0.1)
    except (TypeError, ValueError):
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
    return str(default_runs_root().resolve())


def with_shared_run_store_config(
    config: Optional[Dict[str, Any]],
    run_id: str,
    runs_root: str,
) -> Dict[str, Any]:
    resolved = json.loads(json.dumps(config or {}))
    identity = resolved.setdefault("identity", {})
    if isinstance(identity, dict):
        identity["run_id"] = run_id
    outputs = resolved.setdefault("outputs", {})
    if isinstance(outputs, dict):
        outputs["run_root"] = runs_root
        outputs.setdefault("write_run_store", True)
    return resolved


def write_blueprint_job_mapping(
    run_id: str,
    job_id: str,
    *,
    blueprint_id: str | None = None,
    blueprint_revision: str | None = None,
    web_ui_service: Dict[str, Any] | None = None,
) -> Path:
    run_dir = Path(shared_runs_root()).expanduser() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "job_id": job_id,
        "blueprint_id": blueprint_id,
        "blueprint_revision": blueprint_revision,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if web_ui_service:
        payload["web_ui_service"] = dict(web_ui_service)
    tmp = run_dir / f".job.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(run_dir / "job.json")
    return run_dir / "job.json"


def enrich_blueprint_from_manifest(repo_root: Path, blueprint: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(blueprint)
    manifest = load_optional_manifest(repo_root, enriched)
    if not manifest:
        return enriched
    enriched["type"] = manifest.get("type") or "batch"
    requirements = manifest.get("requirements")
    if isinstance(requirements, dict):
        enriched["requirements"] = {**as_dict(enriched.get("requirements")), **requirements}
    init_config_review = manifest_init_config_review(manifest)
    if init_config_review is not None:
        enriched["init_config_review"] = init_config_review
    return enriched


def load_optional_manifest(repo_root: Path, blueprint: Dict[str, Any]) -> Dict[str, Any]:
    try:
        bundle_root = blueprint_bundle_root(repo_root, blueprint)
    except HTTPException:
        return {}
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return {}
    if not isinstance(manifest, dict):
        return {}
    return expand_blueprint_manifest_if_source(bundle_root, manifest)


def manifest_init_config_review(manifest: Dict[str, Any]) -> Any:
    if "init_config_review" in manifest:
        return manifest.get("init_config_review")
    metadata = manifest.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get("init_config_review")
    return None


def blueprint_runtime_environment(
    bundle_root: Path,
    *,
    config: Dict[str, Any] | None = None,
    config_overrides: Dict[str, Any] | None = None,
) -> Dict[str, str]:
    env: Dict[str, str] = runtime_path_environment()
    if config is None:
        config = load_blueprint_config(bundle_root, config_overrides=config_overrides)
    if config is not None:
        env["MN_BLUEPRINT_CONFIG_JSON"] = json.dumps(config, sort_keys=True)
        projected_config = load_blueprint_config_overwrites(bundle_root, config_overrides=config_overrides)
        if projected_config is not None:
            env.update(config_to_environment(projected_config))
        docker_model_env = resolve_llm_environment(config)
        if docker_model_env:
            env.update(docker_model_env)

    scenario_path = bundle_root / "scenario.json"
    if scenario_path.is_file():
        env["MN_BLUEPRINT_SCENARIO_JSON"] = scenario_path.read_text()
    return env


def apply_manifest_config_bindings(manifest: Dict[str, Any], config: Dict[str, Any]) -> None:
    bindings = config.get("manifest_config_bindings") or []
    if not isinstance(bindings, list):
        return
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        config_path = binding.get("config_path") or binding.get("from")
        manifest_path = binding.get("manifest_path") or binding.get("to")
        if not isinstance(config_path, str) or not isinstance(manifest_path, str):
            continue
        value = config_path_get(config, config_path)
        if value is None and not binding.get("allow_null", False):
            continue
        if binding.get("stringify") is True:
            value = str(value).lower() if isinstance(value, bool) else str(value)
        set_manifest_path(manifest, manifest_path, value)


def config_to_environment(config: Dict[str, Any]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    docker_model_env = resolve_llm_environment(config)
    for path, names in (
        ("video_source.uri", ("VIDEO_SOURCE_URI",)),
        ("video_source.transport", ("VIDEO_SOURCE_TRANSPORT",)),
        ("video_source.codec", ("VIDEO_SOURCE_CODEC",)),
        ("video_source.camera_id", ("VIDEO_SOURCE_CAMERA_ID",)),
        ("video_source.frame_sample_seconds", ("FRAME_SAMPLE_SECONDS",)),
        ("video_source.frame_jpeg_max_width", ("FRAME_JPEG_MAX_WIDTH",)),
        ("vl_model.base_url", ("VL_MODEL_BASE_URL", "OLLAMA_BASE_URL")),
        ("vl_model.model", ("VL_MODEL_NAME", "OLLAMA_MODEL")),
        ("vl_model.timeout_seconds", ("VL_MODEL_TIMEOUT_SECONDS", "OLLAMA_TIMEOUT_SECONDS")),
        ("vl_model.temperature", ("VL_MODEL_TEMPERATURE", "OLLAMA_TEMPERATURE")),
    ):
        value = config_path_get(config, path)
        if value is None:
            continue
        for name in names:
            env[name] = str(value)

    if docker_model_env:
        env.update(docker_model_env)
        return env

    for path, names in (
        ("llm.api_base", ("MN_LLM_API_BASE", "LITELLM_API_BASE")),
        ("llm.model", ("MN_LLM_MODEL", "LITELLM_MODEL")),
        ("llm.timeout_seconds", ("MN_LLM_TIMEOUT_SECONDS", "LITELLM_TIMEOUT_SECONDS")),
        ("llm.max_tokens", ("MN_LLM_MAX_TOKENS", "LITELLM_MAX_TOKENS")),
        ("llm.num_retries", ("MN_LLM_NUM_RETRIES", "LITELLM_NUM_RETRIES")),
    ):
        value = config_path_get(config, path)
        if value is None:
            continue
        for name in names:
            env[name] = str(value)
    return env


def set_manifest_path(target: Any, dotted_path: str, value: Any) -> None:
    parts = [part for part in dotted_path.split(".") if part]
    _set_path(target, parts, value)


def _set_path(cursor: Any, parts: list[str], value: Any) -> None:
    if not parts:
        return
    part = parts[0]
    rest = parts[1:]

    if isinstance(cursor, list):
        for item in _list_targets(cursor, part):
            _set_path(item, rest, value)
        return

    if not isinstance(cursor, dict):
        return

    if len(parts) == 1:
        cursor[part] = value
        return

    next_value = cursor.get(part)
    if isinstance(next_value, list):
        _set_path(next_value, rest, value)
        return
    if not isinstance(next_value, dict):
        next_value = {}
        cursor[part] = next_value
    _set_path(next_value, rest, value)


def _list_targets(items: list[Any], selector: str) -> list[Any]:
    if selector == "*":
        return [item for item in items if isinstance(item, dict)]
    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(items):
            return [items[index]]
        return []
    if selector.endswith("*"):
        prefix = selector[:-1]
        return [
            item
            for item in items
            if isinstance(item, dict) and str(item.get("node_id") or "").startswith(prefix)
        ]
    return [
        item
        for item in items
        if isinstance(item, dict) and (item.get("node_id") == selector or item.get("edge_id") == selector)
    ]


def config_path_get(config: Dict[str, Any], dotted_path: str) -> Any:
    cursor: Any = config
    for part in dotted_path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def load_blueprint_config(
    bundle_root: Path,
    *,
    config_overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    config: Dict[str, Any] = {}
    loaded = False
    for path in (
        bundle_root / "config" / "default.json",
        bundle_root / "config" / "overwrite.json",
    ):
        if path.is_file():
            config = deep_merge(config, read_json_object(path))
            loaded = True
    if config_overrides:
        config = deep_merge(config, config_overrides)
        loaded = True
    return config if loaded else None


def load_blueprint_config_overwrites(
    bundle_root: Path,
    *,
    config_overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    config: Dict[str, Any] = {}
    loaded = False
    overwrite_path = bundle_root / "config" / "overwrite.json"
    if overwrite_path.is_file():
        config = deep_merge(config, read_json_object(overwrite_path))
        loaded = True
    if config_overrides:
        config = deep_merge(config, config_overrides)
        loaded = True
    return config if loaded else None


def inject_node_environment(manifest: Dict[str, Any], env: Dict[str, str]) -> None:
    for node in manifest_agent_nodes(manifest):
        node_config = node.setdefault("config", {})
        if not isinstance(node_config, dict):
            continue
        environment = node_config.setdefault("environment", {})
        if not isinstance(environment, dict):
            continue
        node_env = dict(env)
        if environment.get("PYTHONPATH") and node_env.get("PYTHONPATH"):
            node_env["PYTHONPATH"] = merge_path_values(
                str(environment["PYTHONPATH"]),
                str(node_env["PYTHONPATH"]),
            )
        adjust_llm_environment_for_node(node_env, node)
        environment.update(node_env)
        add_mn_llm_aliases(environment)


def merge_path_values(*values: str) -> str:
    merged: list[str] = []
    for value in values:
        for item in value.split(os.pathsep):
            item = item.strip()
            if item and item not in merged:
                merged.append(item)
    return os.pathsep.join(merged)


def add_mn_llm_aliases(environment: Dict[str, Any]) -> None:
    for legacy, primary in (
        ("LITELLM_MODEL", "MN_LLM_MODEL"),
        ("LITELLM_API_BASE", "MN_LLM_API_BASE"),
        ("LITELLM_API_KEY", "MN_LLM_API_KEY"),
        ("LITELLM_TIMEOUT_SECONDS", "MN_LLM_TIMEOUT_SECONDS"),
        ("LITELLM_MAX_TOKENS", "MN_LLM_MAX_TOKENS"),
        ("LITELLM_NUM_RETRIES", "MN_LLM_NUM_RETRIES"),
        ("LITELLM_RETRY_BACKOFF_SECONDS", "MN_LLM_RETRY_BACKOFF_SECONDS"),
    ):
        if primary not in environment and legacy in environment:
            environment[primary] = environment[legacy]


def adjust_llm_environment_for_node(environment: Dict[str, Any], node: Dict[str, Any]) -> None:
    if environment.get("MN_LLM_PROVIDER") != "docker_model_runner":
        return
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    if config.get("runner_module") == "MirrorNeuron.Runner.HostLocal":
        return
    api_base = str(environment.get("MN_LLM_API_BASE") or "")
    if "localhost:12434" in api_base or "127.0.0.1:12434" in api_base:
        environment["MN_LLM_API_BASE"] = DOCKER_MODEL_RUNNER_CONTAINER_API_BASE


def read_json_object(path: Path) -> Dict[str, Any]:
    try:
        decoded = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"{path.name} is malformed") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=500, detail=f"{path.name} must contain a JSON object")
    return decoded


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
