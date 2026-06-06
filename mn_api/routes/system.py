from __future__ import annotations

import json
import re
import socket
from numbers import Number
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
import grpc
from mn_sdk import Client

from mn_api import state
from mn_api.blueprints import is_git_repo_url, shared_runs_root
from mn_api.config import auth_enabled
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import ClusterNodeAddRequest, ClusterNodeRemoveRequest, ResourceSetRequest


router = APIRouter(prefix="/api/v1")

DEFAULT_NODE_ADD_GRPC_PORTS = (55051, 50051)


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


@router.post("/system/cluster/nodes:add")
@router.post("/system/cluster/nodes:join")
def add_cluster_node(req: ClusterNodeAddRequest, _auth=Depends(require_auth)):
    try:
        host = normalize_node_host(req.host)
        token = normalize_node_token(req.token)
        local_host = detect_lan_ip()
        handshake = network_handshake_with_fallback(
            host=host,
            token=token,
            grpc_ports=candidate_grpc_ports(req.grpc_port),
            local_host=local_host,
        )
        node_name = normalize_node_name(handshake.get("node_name") or f"mirror_neuron@{host}")
        status = state.client.add_node(node_name, token=token)
        return {
            "ok": True,
            "host": host,
            "node_name": node_name,
            "status": status,
            "message": f"{node_name} was added to this box.",
            "handshake": public_handshake_summary(handshake),
        }
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/system/cluster/nodes:remove")
@router.post("/system/cluster/nodes:leave")
def remove_cluster_node(req: ClusterNodeRemoveRequest, _auth=Depends(require_auth)):
    try:
        node_name = normalize_node_name(req.node_name)
        status = state.client.remove_node(node_name)
        return {
            "ok": True,
            "node_name": node_name,
            "status": status,
            "message": f"{node_name} was removed from this box.",
        }
    except HTTPException:
        raise
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
    "gpu_memory_total_mb",
    "gpu_memory_free_mb",
    "gpu_memory_total_gb",
    "gpu_memory_free_gb",
    "memory_gb",
    "memory_total_gb",
    "memory_available_gb",
    "disk_gb",
    "disk_available_gb",
)
INTEGER_RESOURCE_KEYS = {"cpu_cores", "gpu_count"}


def ensure_combined_resource_totals(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload

    enriched = dict(payload)

    if isinstance(enriched.get("nodes"), list):
        enriched["nodes"] = [
            normalize_resource_totals(node) if isinstance(node, dict) else node
            for node in enriched["nodes"]
        ]

    if isinstance(enriched.get("totals"), dict):
        enriched["totals"] = normalize_resource_totals(enriched["totals"])

    if isinstance(enriched.get("usable"), dict):
        enriched["usable"] = normalize_resource_totals(enriched["usable"])

    if isinstance(enriched.get("combined"), dict):
        combined = enriched["combined"]
    elif isinstance(enriched.get("totals"), dict):
        combined = enriched["totals"]
    elif isinstance(enriched.get("nodes"), list):
        combined = combine_node_resources(enriched["nodes"])
    else:
        return enriched

    enriched["combined"] = normalize_resource_totals(combined)
    return enriched


def combine_node_resources(nodes: Any) -> dict[str, Any]:
    combined: dict[str, float] = {key: 0.0 for key in RESOURCE_TOTAL_KEYS}

    if not isinstance(nodes, list):
        return combined

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node = normalize_resource_totals(node)
        for key in RESOURCE_TOTAL_KEYS:
            combined[key] += resource_number(node.get(key))

    return normalize_resource_totals(combined)


def normalize_resource_totals(totals: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(totals)

    if "memory_total_gb" not in normalized and "memory_gb" in normalized:
        normalized["memory_total_gb"] = normalized["memory_gb"]
    if "memory_gb" not in normalized and "memory_total_gb" in normalized:
        normalized["memory_gb"] = normalized["memory_total_gb"]
    if "memory_available_gb" not in normalized:
        normalized["memory_available_gb"] = 0.0

    if "gpu_memory_total_gb" not in normalized and "gpu_memory_total_mb" in normalized:
        normalized["gpu_memory_total_gb"] = resource_number(normalized["gpu_memory_total_mb"]) / 1024
    if "gpu_memory_free_gb" not in normalized and "gpu_memory_free_mb" in normalized:
        normalized["gpu_memory_free_gb"] = resource_number(normalized["gpu_memory_free_mb"]) / 1024
    if "gpu_memory_total_mb" not in normalized and "gpu_memory_total_gb" in normalized:
        normalized["gpu_memory_total_mb"] = resource_number(normalized["gpu_memory_total_gb"]) * 1024
    if "gpu_memory_free_mb" not in normalized and "gpu_memory_free_gb" in normalized:
        normalized["gpu_memory_free_mb"] = resource_number(normalized["gpu_memory_free_gb"]) * 1024

    for key in RESOURCE_TOTAL_KEYS:
        if key not in totals:
            if key not in normalized:
                normalized[key] = 0 if key in INTEGER_RESOURCE_KEYS else 0.0
            value = resource_number(normalized.get(key))
            normalized[key] = int(value) if key in INTEGER_RESOURCE_KEYS else round(value, 2)
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


def normalize_node_host(value: str) -> str:
    host = str(value or "").strip()
    if (
        not host
        or len(host) > 253
        or host.startswith("-")
        or re.search(r"\s", host)
        or re.match(r"^[a-z][a-z0-9+.-]*://", host, flags=re.IGNORECASE)
    ):
        raise HTTPException(status_code=422, detail="Remote node host must be a host name or IP address.")
    return host


def normalize_node_token(value: str) -> str:
    token = str(value or "").strip()
    if not token or len(token) > 4096 or re.search(r"\s", token):
        raise HTTPException(status_code=422, detail="Remote node token is required.")
    return token


def normalize_node_name(value: str) -> str:
    node_name = str(value or "").strip()
    if (
        not node_name
        or len(node_name) > 253
        or node_name.startswith("-")
        or re.search(r"\s", node_name)
        or "@" not in node_name
    ):
        raise HTTPException(status_code=422, detail="Remote node name must look like mirror_neuron@host.")
    return node_name


def normalize_grpc_port(value: int | None) -> int:
    try:
        port = int(value or 55051)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Remote gRPC port must be a valid TCP port.")
    if port < 1 or port > 65535:
        raise HTTPException(status_code=422, detail="Remote gRPC port must be a valid TCP port.")
    return port


def candidate_grpc_ports(value: int | None) -> list[int]:
    if value is not None:
        return [normalize_grpc_port(value)]
    return list(DEFAULT_NODE_ADD_GRPC_PORTS)


def network_handshake_with_fallback(
    *,
    host: str,
    token: str,
    grpc_ports: list[int],
    local_host: str,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for grpc_port in grpc_ports:
        try:
            remote = Client(target=f"{host}:{grpc_port}", auth_token="", timeout=10)
            return remote.network_handshake(
                token,
                node_name=network_node_name(local_host),
                node_info=handshake_node_info(local_host),
            )
        except Exception as exc:
            last_error = exc
            if not is_grpc_unavailable(exc):
                raise
    if last_error:
        raise last_error
    raise HTTPException(status_code=422, detail="Remote gRPC port must be a valid TCP port.")


def is_grpc_unavailable(error: Exception) -> bool:
    return isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.UNAVAILABLE


def loopback_host(value: str) -> bool:
    host = str(value or "").strip().lower()
    return host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def detect_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        detected = probe.getsockname()[0]
        if not loopback_host(detected):
            return detected
    except OSError:
        pass
    finally:
        probe.close()

    try:
        detected = socket.gethostbyname(socket.gethostname())
        if not loopback_host(detected):
            return detected
    except OSError:
        pass

    return "127.0.0.1"


def network_node_name(host: str) -> str:
    return f"mirror_neuron@{host}"


def handshake_node_info(local_host: str) -> dict[str, Any]:
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""

    return {
        "node_name": network_node_name(local_host),
        "display_name": hostname or local_host,
        "hostname": hostname,
    }


def public_handshake_summary(handshake: dict[str, Any]) -> dict[str, Any]:
    public_keys = (
        "node_name",
        "runtime_mode",
        "grpc_host",
        "grpc_port",
        "cluster_nodes",
        "network_only",
    )
    return {key: handshake[key] for key in public_keys if key in handshake}
