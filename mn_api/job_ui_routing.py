"""Core-owned service placement for the bounded Job UI relay."""

import json
from urllib.parse import urlsplit

from fastapi import HTTPException

from mn_api import state

EXECUTION_HEADER = "X-MN-UI-Execution"
HOP_HEADER = "X-MN-UI-Hop"


def _record(value):
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def node_api_base_url(node):
    """Use the peer's registered endpoint, independently of our listener."""
    host = _registered_host(node)
    hardware = node.get("hardware")
    endpoint = hardware.get("http_api") if isinstance(hardware, dict) else None
    if (not isinstance(endpoint, dict) or endpoint.get("schema_version") != "mn.http_api.endpoint.v1"
            or endpoint.get("enabled") is not True or endpoint.get("scheme") not in {"http", "https"}
            or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535):
        raise HTTPException(503, "The computer hosting this Web UI has no registered API endpoint. Update its runtime.")
    return f"{endpoint['scheme']}://{'[' + host + ']' if ':' in host else host}:{endpoint['port']}/api/v1"


def _registered_host(node):
    if not isinstance(node, dict):
        raise HTTPException(503, "The computer hosting this Web UI has no valid address.")
    host = str(node.get("address") or node.get("grpc_host") or "")
    try:
        parsed = urlsplit("http://" + (f"[{host}]" if ":" in host else host))
        valid = parsed.hostname and parsed.hostname == host and not parsed.username and not parsed.path and not parsed.query and not parsed.fragment
    except ValueError:
        valid = False
    if not valid or host in {"0.0.0.0", "::"}:
        raise HTTPException(503, "The computer hosting this Web UI has no valid address.")
    return host


def placement(job_id, job=None):
    job = _record(state.client.get_job(job_id)) if job is None else job
    execution_id = str(job.get("latest_run_id") or "").strip()
    owner = str(job.get("owner_node") or "").strip()
    if not execution_id or not owner:
        raise HTTPException(409, "The job has no current UI service placement.")
    summary = _record(state.client.get_system_summary())
    node = next((node for node in summary.get("nodes", [])
                 if node.get("name") == owner), None)
    if not node:
        raise HTTPException(503, "The computer hosting this Web UI is unavailable.")
    # Only Core membership may select the peer, never a handle or query value.
    host = _registered_host(node)
    local = node.get("self?") is True or node.get("self") is True
    return {"execution_id": execution_id, "owner_node": owner,
            "local": local, "host": host,
            **({"api_base_url": node_api_base_url(node)} if not local else {})}


def routed_handle(handle):
    ui = dict(handle["web_ui"])
    # Transport authority is generated here, never accepted from a registration,
    # including when an idle Job has no placement yet.
    ui.pop("transport", None)
    job = _record(state.client.get_job(handle["job_id"]))
    if not str(job.get("owner_node") or "").strip():
        return {**handle, "web_ui": ui}
    location = placement(handle["job_id"], job)
    metadata = ui.get("metadata") or {}
    if metadata.get("execution_id") and metadata["execution_id"] != location["execution_id"]:
        ui["status"] = "stopped"
        ui["metadata"] = {**metadata, "readiness": {"ready": False, "reason": "stale_execution"}}
    if not location["local"]:
        ui["transport"] = {
            "schema_version": "mn.web_ui.transport.v1",
            "owner_api_url": location["api_base_url"],
            "execution_id": location["execution_id"],
        }
    return {**handle, "web_ui": ui}


def relay_target(web_ui, job_id, port, path, query, *, websocket=False):
    """Return an owner relay target after the ordinary port/lifecycle checks."""
    from mn_api.web_ui_server import _safe_proxy_path

    transport = web_ui.get("transport")
    if not isinstance(transport, dict):
        return None
    if transport.get("schema_version") != "mn.web_ui.transport.v1" or not transport.get("execution_id"):
        raise HTTPException(502, "The job UI transport is invalid.")
    base = str(transport.get("owner_api_url") or "")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/api/v1":
        raise HTTPException(502, "The job UI transport is invalid.")
    from urllib.parse import quote

    suffix = ("ws/" if websocket else "") + _safe_proxy_path(path)
    target = f"{base}/jobs/{quote(job_id, safe='-._')}/ui-proxy/{port}/{suffix}"
    if websocket:
        target = ("wss" if parsed.scheme == "https" else "ws") + target[target.index(":"):]
    return target + ("?" + query if query else "")
