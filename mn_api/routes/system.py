from __future__ import annotations

import re
import socket
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
import grpc
from mn_sdk import (
    Client,
    RuntimeConfig,
    RuntimeService,
    collect_runtime_status,
    docker_status,
    ensure_combined_resource_totals,
    health_report_from_status,
    litellm_gateway_health,
)

from mn_api import state
from mn_api.blueprints import shared_runs_root
from mn_api.config import auth_enabled
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import (
    ClusterNodeAddRequest,
    ClusterNodeRemoveRequest,
    NodeDrainRequest,
    NodeMaintenanceRequest,
    NodeReconcileRequest,
    NodeUndrainRequest,
    ResourceSetRequest,
)


router = APIRouter(prefix="/api/v1")

DEFAULT_NODE_ADD_GRPC_PORTS = (55051, 50051)


@router.get("/health")
def health():
    config = state.refresh_config_from_env()
    return {
        "status": "ok",
        "auth": "enabled" if auth_enabled(config) else "disabled",
        "env": getattr(config, "env", ""),
        "blueprint_source": getattr(config, "blueprint_source", ""),
        "blueprint_repo": getattr(config, "blueprint_repo", ""),
        "blueprint_local": getattr(config, "blueprint_local", ""),
        "active_blueprint_location": getattr(config, "active_blueprint_location", ""),
        "runs_root": shared_runs_root(),
    }


@router.get("/runtime/status")
def runtime_status(timeout: float = 3.0, _auth=Depends(require_auth)):
    return collect_runtime_status(
        config=RuntimeConfig.from_env(),
        client=state.client,
        timeout=timeout,
        web_ui_installed=None,
    )


@router.get("/runtime/health")
def runtime_health(timeout: float = 3.0, _auth=Depends(require_auth)):
    return health_report_from_status(runtime_status(timeout=timeout, _auth=_auth))


@router.get("/runtime/doctor")
def runtime_doctor(timeout: float = 3.0, _auth=Depends(require_auth)):
    status = runtime_status(timeout=timeout, _auth=_auth)
    foundation = [
        _foundation_component("docker_model_runner", docker_status),
        _foundation_component("litellm_gateway", lambda: litellm_gateway_health(timeout=timeout)),
    ]
    components = list(status.get("components") or []) + foundation
    overall = _overall_status(components)
    return {
        "version": status.get("version", 1),
        "overall": overall,
        "checked_at": status.get("checked_at"),
        "runtime": status.get("runtime") or {},
        "endpoints": status.get("endpoints") or {},
        "components": components,
        "foundation": {component["name"]: component for component in foundation},
        "nodes": status.get("nodes") or {},
        "jobs": status.get("jobs") or {},
        "shared_storage": status.get("shared_storage") or {},
    }


@router.get("/system/summary")
def get_system_summary(_auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).system_summary()
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/nodes")
def get_nodes(_auth=Depends(require_auth)):
    summary = get_system_summary(_auth=_auth)
    if isinstance(summary, dict):
        return _strip_restart_history(summary)
    return summary


@router.get("/metrics")
def get_metrics(_auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).metrics()
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/resource")
def get_resource(_auth=Depends(require_auth)):
    try:
        resource = RuntimeService(state.client).get_resource()
        if isinstance(resource, dict):
            enriched = ensure_combined_resource_totals(resource)
            if isinstance(enriched, dict):
                enriched["native_ports"] = native_service_ports()
            return enriched
        return resource
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/resource/ports")
def get_resource_ports(_auth=Depends(require_auth)):
    return {"ports": native_service_ports()}


@router.post("/resource")
@router.put("/resource")
def set_resource(req: ResourceSetRequest, _auth=Depends(require_auth)):
    try:
        if hasattr(req, "model_dump"):
            payload = req.model_dump(exclude_none=True)
        else:
            payload = req.dict(exclude_none=True)
        payload.pop("version", None)
        return RuntimeService(state.client).set_resource(payload)
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


@router.post("/nodes/{node_name}/reconcile")
def reconcile_node(node_name: str, req: NodeReconcileRequest | None = None, _auth=Depends(require_auth)):
    request = req or NodeReconcileRequest()
    try:
        return RuntimeService(state.client).reconcile_node(
            normalize_node_name(node_name),
            reason=request.reason,
            dry_run=request.dry_run,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/nodes/{node_name}/drain")
def drain_node(node_name: str, req: NodeDrainRequest | None = None, _auth=Depends(require_auth)):
    request = req or NodeDrainRequest()
    try:
        return RuntimeService(state.client).drain_node(
            normalize_node_name(node_name),
            reason=request.reason,
            deadline=request.deadline,
            deadline_ms=request.deadline_ms,
            dry_run=request.dry_run,
            wait=request.wait,
            ignore_system_jobs=request.ignore_system_jobs,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/nodes/{node_name}/undrain")
def undrain_node(node_name: str, req: NodeUndrainRequest | None = None, _auth=Depends(require_auth)):
    request = req or NodeUndrainRequest()
    try:
        return RuntimeService(state.client).undrain_node(
            normalize_node_name(node_name),
            reason=request.reason,
            mark_eligible=request.mark_eligible,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/nodes/{node_name}/maintenance")
def set_node_maintenance(node_name: str, req: NodeMaintenanceRequest | None = None, _auth=Depends(require_auth)):
    request = req or NodeMaintenanceRequest()
    try:
        return RuntimeService(state.client).set_node_maintenance(
            normalize_node_name(node_name),
            enabled=request.enabled,
            reason=request.reason,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


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
        port = 55051 if value is None else int(value)
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
    remote_client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    client_factory = remote_client_factory or Client
    last_error: Exception | None = None
    for grpc_port in grpc_ports:
        try:
            remote = client_factory(target=f"{host}:{grpc_port}", auth_token="", timeout=10)
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


def native_service_ports() -> list[dict[str, str]]:
    config = RuntimeConfig.from_env()
    endpoints = [
        ("core_grpc", config.grpc_target, "gRPC runtime"),
        ("api", config.api_base_url, "REST API"),
        ("web_ui", config.web_ui_url, "Web UI"),
    ]
    ports: list[dict[str, str]] = []
    for name, target, label in endpoints:
        host, port = _host_port_from_target(target)
        if not port:
            continue
        ports.append({"name": name, "label": label, "host": host, "port": port, "target": target})
    return ports


def _host_port_from_target(target: str | None) -> tuple[str, str]:
    text = str(target or "").strip()
    if not text:
        return "", ""
    if "://" in text:
        parsed = urllib_parse_url(text)
        return parsed["host"], parsed["port"]
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host, port
    return text, ""


def urllib_parse_url(value: str) -> dict[str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    return {"host": parsed.hostname or "", "port": str(parsed.port or "")}


def _foundation_component(name: str, probe: Callable[[], Any]) -> dict[str, Any]:
    try:
        payload = probe()
        if isinstance(payload, dict):
            status = str(payload.get("status") or payload.get("overall") or "")
            if status in {"passing", "warning", "critical"}:
                component_status = status
            else:
                component_status = "passing" if payload.get("ok") is True or payload.get("available") is True else "warning"
            return {"name": name, "status": component_status, "detail": payload}
        return {"name": name, "status": "passing", "detail": payload}
    except Exception as exc:
        return {"name": name, "status": "critical", "error": str(exc)}


def _overall_status(components: list[dict[str, Any]]) -> str:
    statuses = {str(component.get("status")) for component in components}
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    return "passing"


def _strip_restart_history(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_restart_history(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _strip_restart_history(item)
        for key, item in value.items()
        if not _restart_history_key(key)
    }


def _restart_history_key(key: object) -> bool:
    normalized = "".join(char for char in str(key).lower() if char.isalnum())
    return bool(
        normalized
        and (
            normalized
            in {"restarthistory", "restartreason", "restartexhaustedreason", "exhaustedreason"}
            or ("restart" in normalized and ("history" in normalized or "reason" in normalized))
        )
    )
