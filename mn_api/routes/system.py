from __future__ import annotations

import json
from numbers import Number
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from mn_api import state
from mn_api.blueprints import is_git_repo_url, shared_runs_root
from mn_api.config import auth_enabled
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import ResourceSetRequest


router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health():
    configured_blueprint_repo = getattr(state.config, "blueprint_repo", "")
    base_blueprint_repo = getattr(state.config, "configured_blueprint_repo", configured_blueprint_repo)
    dev_local_blueprint_repo = getattr(state.config, "dev_local_blueprint_repo", "")
    blueprint_repo = (
        configured_blueprint_repo
        if is_git_repo_url(configured_blueprint_repo)
        else str(Path(configured_blueprint_repo).expanduser().resolve()) if configured_blueprint_repo else ""
    )
    configured_repo = (
        base_blueprint_repo
        if is_git_repo_url(base_blueprint_repo)
        else str(Path(base_blueprint_repo).expanduser().resolve()) if base_blueprint_repo else ""
    )
    dev_repo = str(Path(dev_local_blueprint_repo).expanduser().resolve()) if dev_local_blueprint_repo else ""
    return {
        "status": "ok",
        "auth": "enabled" if auth_enabled(state.config) else "disabled",
        "blueprint_repo": blueprint_repo,
        "blueprint_repo_mode": "remote" if is_git_repo_url(configured_blueprint_repo) else "local",
        "configured_blueprint_repo": configured_repo,
        "dev_local_blueprint_repo": dev_repo,
        "dev_local_blueprint_repo_active": bool(dev_repo and dev_repo == blueprint_repo),
        "runs_root": shared_runs_root(),
    }


@router.get("/system/summary")
def get_system_summary(_auth=Depends(require_auth)):
    try:
        summary_json = state.client.get_system_summary()
        return json.loads(summary_json)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/metrics")
def get_metrics(_auth=Depends(require_auth)):
    try:
        summary = json.loads(state.client.get_system_summary())
        if "metrics" in summary:
            return summary["metrics"]

        jobs = summary.get("jobs", [])
        return {
            "jobs": {
                "total": len(jobs),
                "by_status": counts(job.get("status", "unknown") for job in jobs),
            },
            "nodes": {"total": len(summary.get("nodes", []))},
            "source": "system_summary",
        }
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/resource")
def get_resource(_auth=Depends(require_auth)):
    try:
        resource = json.loads(state.client.get_resource())
        return ensure_combined_resource_totals(resource)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/resource")
@router.put("/resource")
def set_resource(req: ResourceSetRequest, _auth=Depends(require_auth)):
    try:
        if hasattr(req, "model_dump"):
            payload = req.model_dump(exclude_none=True)
        else:
            payload = req.dict(exclude_none=True)
        resource = json.loads(state.client.set_resource(payload))
        return ensure_combined_resource_totals(resource)
    except Exception as exc:
        return handle_grpc_error(exc)


def counts(values):
    result = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


RESOURCE_TOTAL_KEYS = (
    "cpu_cores",
    "gpu_count",
    "memory_gb",
    "disk_gb",
    "disk_available_gb",
)
INTEGER_RESOURCE_KEYS = {"cpu_cores", "gpu_count"}


def ensure_combined_resource_totals(payload: Any) -> Any:
    if not isinstance(payload, dict) or isinstance(payload.get("combined"), dict):
        return payload

    if isinstance(payload.get("totals"), dict):
        combined = payload["totals"]
    elif isinstance(payload.get("nodes"), list):
        combined = combine_node_resources(payload["nodes"])
    else:
        return payload

    enriched = dict(payload)
    enriched["combined"] = normalize_resource_totals(combined)
    return enriched


def combine_node_resources(nodes: Any) -> dict[str, Any]:
    combined: dict[str, float] = {key: 0.0 for key in RESOURCE_TOTAL_KEYS}

    if not isinstance(nodes, list):
        return combined

    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in RESOURCE_TOTAL_KEYS:
            combined[key] += resource_number(node.get(key))

    return normalize_resource_totals(combined)


def normalize_resource_totals(totals: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(totals)
    for key in RESOURCE_TOTAL_KEYS:
        if key not in totals:
            continue
        value = resource_number(totals.get(key))
        normalized[key] = int(value) if key in INTEGER_RESOURCE_KEYS else round(value, 2)
    return normalized


def resource_number(value: Any) -> float:
    if isinstance(value, Number):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0
