from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Optional

from fastapi import HTTPException
from mn_sdk import (
    make_validation_report,
    run_input_validation,
    validate_input_validation_spec_issues,
    validate_requirements_spec_issues,
)

from mn_api.config import ApiConfig
from mn_api.path_utils import inside_path


BLUEPRINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,220}$")
CATEGORY_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
DEFAULT_CATEGORY = "General"
DEFAULT_RUNS_ROOT = "~/.mn/runs"


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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


def blueprint_repo_root(config: ApiConfig) -> Path:
    repo_value = getattr(config, "blueprint_repo", "")
    if not repo_value:
        raise HTTPException(status_code=500, detail="MN_BLUEPRINT_REPO is not configured")

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
    force: bool = False,
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
    runs_root = shared_runs_root()
    config = with_shared_run_store_config(
        load_blueprint_config(bundle_root, config_overrides=config_overrides),
        run_id,
        runs_root,
    )
    if config is not None:
        apply_manifest_config_bindings(manifest, config)
    runtime_env = blueprint_runtime_environment(
        bundle_root,
        config=config,
        config_overrides=config_overrides,
    )
    runtime_env.setdefault("MN_RUN_ID", run_id)
    runtime_env["MN_RUNS_ROOT"] = runs_root
    if runtime_env:
        inject_node_environment(manifest, runtime_env)

    payloads: Dict[str, bytes] = {}
    if payloads_path.is_dir():
        for payload_path in payloads_path.rglob("*"):
            if payload_path.is_file():
                payloads[payload_path.relative_to(payloads_path).as_posix()] = payload_path.read_bytes()

    return json.dumps(manifest), payloads


def validate_blueprint_inputs(
    repo_root: Path,
    blueprint: Dict[str, Any],
    *,
    config_overrides: Dict[str, Any] | None = None,
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
    env: Dict[str, str] = {}
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
        environment.update(env)
        add_mn_llm_aliases(environment)


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
