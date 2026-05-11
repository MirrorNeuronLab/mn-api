from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Dict, Optional

from fastapi import HTTPException

from mn_api.config import ApiConfig
from mn_api.path_utils import inside_path


BLUEPRINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,220}$")


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
            blueprints.append(normalized)
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
        "category": record.get("category") or product.get("category") or "General",
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


def load_blueprint_bundle(repo_root: Path, blueprint: Dict[str, Any], run_id: str) -> tuple[str, Dict[str, bytes]]:
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
    if blueprint.get("revision"):
        metadata["blueprint_revision"] = blueprint["revision"]
    manifest["run_id"] = run_id

    payloads: Dict[str, bytes] = {}
    if payloads_path.is_dir():
        for payload_path in payloads_path.rglob("*"):
            if payload_path.is_file():
                payloads[payload_path.relative_to(payloads_path).as_posix()] = payload_path.read_bytes()

    return json.dumps(manifest), payloads
