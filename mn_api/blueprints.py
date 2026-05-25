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
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from mn_sdk import (
    make_validation_report,
    run_input_validation,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
)

from mn_api.config import ApiConfig, runtime_env_values
from mn_api.path_utils import inside_path


BLUEPRINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,220}$")
CATEGORY_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
DEFAULT_CATEGORY = "General"
DEFAULT_RUNS_ROOT = "~/.mn/runs"
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
    for name in ("MN_WORKSPACE_ROOT", "MIRROR_NEURON_WORKSPACE", "OTTERDESK_MIRROR_NEURON_WORKSPACE"):
        value = os.getenv(name)
        if value:
            return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def runtime_path_environment() -> Dict[str, str]:
    root = workspace_root()
    membrane_project_path = Path(os.getenv("MN_MEMBRANE_PROJECT_PATH") or root / "Membrane").expanduser()
    membrane_sdk_path = Path(
        os.getenv("MN_MEMBRANE_SDK_PATH")
        or os.getenv("MN_CONTEXT_PYTHON_SDK_PATH")
        or membrane_project_path / "mn-context-engine-python-sdk" / "src"
    ).expanduser()
    skills_root = Path(os.getenv("MN_SKILLS_ROOT") or root / "mn-skills").expanduser()
    env = {
        "MN_WORKSPACE_ROOT": str(root),
        "MIRROR_NEURON_WORKSPACE": str(root),
        "OTTERDESK_MIRROR_NEURON_WORKSPACE": str(root),
        "MN_MEMBRANE_PROJECT_PATH": str(membrane_project_path),
        "MN_MEMBRANE_SDK_PATH": str(membrane_sdk_path),
        "MN_SKILLS_ROOT": str(skills_root),
    }
    python_paths = [
        skills_root / "blueprint_support_skill" / "src",
        skills_root / "tax_pdf_ocr_skill" / "src",
        skills_root / "pdf_extract_skill" / "src",
    ]
    existing_pythonpath = os.getenv("PYTHONPATH")
    resolved_python_paths = [str(path) for path in python_paths if path.exists()]
    if existing_pythonpath:
        resolved_python_paths.append(existing_pythonpath)
    if resolved_python_paths:
        env["PYTHONPATH"] = os.pathsep.join(resolved_python_paths)
    return env


def inject_local_blueprint_support_path() -> None:
    skills_root = Path(runtime_path_environment()["MN_SKILLS_ROOT"]).expanduser()
    for candidate in (
        skills_root / "blueprint_support_skill" / "src",
        skills_root / "blueprint-support-skill" / "src",
    ):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            return


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
        value = os.getenv(key) or runtime_env.get(key)
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


def create_blueprint_run_id(blueprint_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{blueprint_id}-{stamp}"


def is_git_repo_url(value: str) -> bool:
    value = (value or "").strip()
    if re.fullmatch(r"[\w.-]+@[\w.-]+:[^\s]+", value):
        return True
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme in {"http", "https", "ssh", "git"} and bool(parsed.netloc)


def cached_git_repo_path(repo_url: str) -> Path:
    parsed = urlparse(repo_url)
    name = Path(parsed.path.rstrip("/")).stem or "blueprints"
    digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
    configured_cache = Path(os.getenv("MN_BLUEPRINT_REPO_CACHE", DEFAULT_BLUEPRINT_REPO_CACHE)).expanduser()
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
    repo_value = getattr(config, "blueprint_repo", "")
    if not repo_value:
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_REPO is not configured")

    if is_git_repo_url(repo_value):
        return ensure_git_blueprint_repo(repo_value)

    repo_root = Path(repo_value).expanduser().resolve()
    if not repo_root.is_dir():
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_REPO is not a directory")
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
        "graph_id": record.get("graph_id") or record.get("graphId") or product.get("graph_id") or "",
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
            "skipped_checks": ["input_validation", "requirements"],
        }
    if blueprint.get("revision"):
        metadata["blueprint_revision"] = blueprint["revision"]
    manifest["run_id"] = run_id
    render_agent_templates_for_submission(manifest)
    prepare_openshell_custom_images(bundle_root, manifest)
    runs_root = shared_runs_root()
    config = with_shared_run_store_config(
        load_blueprint_config(bundle_root, config_overrides=config_overrides),
        run_id,
        runs_root,
    )
    if config is not None:
        apply_manifest_config_bindings(manifest, config)
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
    if runtime_env:
        inject_node_environment(manifest, runtime_env)

    payloads: Dict[str, bytes] = {}
    if payloads_path.is_dir():
        for payload_path in payloads_path.rglob("*"):
            if payload_path.is_file():
                payloads[payload_path.relative_to(payloads_path).as_posix()] = payload_path.read_bytes()
    payloads.update(runtime_web_ui_support_payloads_for_manifest(manifest))
    stage_local_input_payloads_for_manifest(manifest, payloads, bundle_dir=bundle_root)

    return json.dumps(manifest), payloads


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
    inject_local_blueprint_support_path()
    try:
        from mn_blueprint_support import inject_runtime_web_ui_service
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="blueprint web UI service injection requires mn_blueprint_support",
        ) from exc
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def runtime_web_ui_support_payloads_for_manifest(manifest: Dict[str, Any]) -> Dict[str, bytes]:
    inject_local_blueprint_support_path()
    try:
        from mn_blueprint_support import runtime_web_ui_service_from_manifest, runtime_web_ui_support_payloads
    except ImportError:
        return {}
    if not runtime_web_ui_service_from_manifest(manifest):
        return {}
    return runtime_web_ui_support_payloads()


def stage_local_input_payloads_for_manifest(
    manifest: Dict[str, Any],
    payloads: Dict[str, bytes],
    *,
    bundle_dir: Path,
) -> Dict[str, Any]:
    inject_local_blueprint_support_path()
    try:
        from mn_blueprint_support import stage_local_input_payloads_for_manifest as stage_payloads
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="local blueprint input staging requires mn_blueprint_support",
        ) from exc
    try:
        return stage_payloads(manifest, payloads, bundle_dir=bundle_dir)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def prepare_openshell_custom_images(bundle_root: Path, manifest: Dict[str, Any]) -> None:
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list):
        return

    for node in nodes:
        if not isinstance(node, dict):
            continue
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
    return Path(os.getenv("OPENSHELL_CONFIG_DIR", str(Path.home() / ".config" / "openshell"))).expanduser()


def openshell_gateway_name() -> str:
    configured = os.getenv("OPENSHELL_GATEWAY", "").strip()
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
    env = os.environ.copy()
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
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list) or not any(isinstance(node, dict) and "uses" in node for node in nodes):
        return
    inject_local_blueprint_support_path()
    try:
        from mn_blueprint_support import render_manifest_agent_templates
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="blueprint uses agent templates but mn_blueprint_support is not installed") from exc
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
    env = os.environ.copy()
    env.update(runtime_env)
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
        raise HTTPException(status_code=500, detail=f"failed to start blueprint pre-launch hook: {exc}") from exc

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
        raise HTTPException(status_code=500, detail=str(exc)) from exc
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
        timeout = max(float(os.getenv("MN_PRE_LAUNCH_TIMEOUT_SECONDS", "30")), 0)
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
        return max(float(os.getenv("MN_PROCESS_CLEANUP_TIMEOUT_SECONDS", str(PROCESS_CLEANUP_TIMEOUT_SECONDS))), 0.1)
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

    env = os.environ.copy()
    env.update(post_launch_env(run_dir, hook_info, reason=reason))
    try:
        timeout = max(float(os.getenv("MN_POST_LAUNCH_TIMEOUT_SECONDS", "10")), 0.1)
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
    if os.getenv("MN_RUN_BACKGROUND_EVENT_RELAY", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError:
        return
    service_info = runtime_web_ui_service_from_manifest(manifest)
    if not service_info:
        return

    bundle_root = validate_blueprint_bundle(repo_root, blueprint)
    runs_root = Path(shared_runs_root()).expanduser()
    run_dir = runs_root / run_id
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
        "mn_blueprint_support.event_relay",
        "--job-id",
        job_id,
        "--run-dir",
        str(run_dir),
        "--poll-seconds",
        f"{poll_seconds:g}",
    ]
    if max_seconds is not None:
        command.extend(["--max-seconds", f"{max_seconds:g}"])

    env = os.environ.copy()
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
    raw = os.getenv("MN_RUN_EVENT_RELAY_POLL_SECONDS")
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
    raw = os.getenv("MN_RUN_EVENT_RELAY_MAX_SECONDS")
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

    spec_issues = validate_requirements_spec_issues(manifest) + validate_input_validation_spec_issues(manifest)
    if spec_issues:
        return make_validation_report(spec_issues)

    config = load_blueprint_config(bundle_root, config_overrides=config_overrides)
    env = blueprint_runtime_environment(
        bundle_root,
        config=config,
        config_overrides=config_overrides,
    )
    env.update(string_env_values(env_overrides))
    return run_input_validation(bundle_root, manifest, config=config, env=env)


def shared_runs_root() -> str:
    return str(os.getenv("MN_RUNS_ROOT") or DEFAULT_RUNS_ROOT)


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
    return manifest if isinstance(manifest, dict) else {}


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
    for node in manifest.get("nodes") or []:
        if not isinstance(node, dict):
            continue
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
