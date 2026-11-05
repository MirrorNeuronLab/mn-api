from __future__ import annotations

import json
import re
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException

from mn_api import state
from mn_api.dependencies import require_auth
from mn_api.job_store import job_data_dir_from_id, shared_job_ui_dir_from_id
from mn_api.job_ui_readiness import probe_job_web_ui
from mn_api.run_store import first_string as _first_string
from mn_api.run_store import read_json_file as _read_json_file


def get_job_ui(job_id: str, _auth=Depends(require_auth), check_readiness: bool = True):
    job_dir = job_data_dir_from_id(job_id, must_exist=False)
    if job_dir is None:
        raise HTTPException(status_code=400, detail="invalid job id")
    live_handle = _job_ui_handle_from_services(job_id)
    shared_dir = shared_job_ui_dir_from_id(job_id, must_exist=False)
    if live_handle is not None:
        # Lifecycle/address come from the registry. Preserve the blueprint's
        # claimed presentation policy only when it describes that same page.
        for candidate in (shared_dir, job_dir):
            claimed = _job_ui_handle_from_directory(candidate, job_id)
            if claimed is not None and str(claimed["web_ui"].get("url") or "").rstrip("/") == live_handle["web_ui"][
                "url"
            ].rstrip("/"):
                policy = claimed["web_ui"].get("metadata")
                if isinstance(policy, dict) and "load_event" in policy:
                    live_handle["web_ui"]["metadata"]["load_event"] = policy["load_event"]
                break
        return _present_job_ui(live_handle, check_readiness)
    # The owner has the current lifecycle and address. Shared copies may still
    # describe a starting or previous attempt while replication catches up.
    remote_handle = _owner_node_job_ui_handle(job_id)
    if remote_handle is not None:
        return _present_job_ui(remote_handle, check_readiness)
    for candidate in (shared_dir, job_dir):
        handle = _job_ui_handle_from_directory(candidate, job_id)
        if handle is not None:
            return _present_job_ui(handle, check_readiness)
    raise HTTPException(status_code=404, detail="job UI not found")


def _present_job_ui(handle, check_readiness):
    from mn_api.job_ui_routing import routed_handle

    handle = routed_handle(handle)
    return _checked_job_ui_handle(handle) if check_readiness else handle


def _checked_job_ui_handle(handle: dict[str, Any]) -> dict[str, Any]:
    """Verify reachability from the proxy's host, not just the worker's host."""
    web_ui = dict(handle["web_ui"])
    if web_ui.get("adapter") not in (None, "external-url"):
        return handle
    status = str(web_ui.get("status") or "").strip().lower()
    raw_metadata = web_ui.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    if status in {"running", "ready", "passing"}:
        readiness = probe_job_web_ui(str(handle["job_id"]), str(web_ui.get("url") or ""))
        web_ui["status"] = "running" if readiness["ready"] else "starting"
    else:
        readiness = {"schema_version": "mn.web_ui.readiness.v1", "ready": False, "reason": "service_not_running"}
    metadata["readiness"] = readiness
    web_ui["metadata"] = metadata
    return {**handle, "web_ui": web_ui}


def _job_ui_handle_from_services(job_id: str) -> dict[str, Any] | None:
    """Project a passing Job UI from the Core registry and node advertisement."""
    if not hasattr(state.client, "list_services"):
        return None
    try:
        job = _json_object(state.client.get_job(job_id))
        run_id = str(job.get("latest_run_id") or "").strip()
        owner_node = _first_string(job.get("owner_node"))
        if not run_id or not owner_node:
            return None
        # An unscoped registry query fans out to every federation peer. UI
        # descriptor reads must not wait on peers unrelated to this Job.
        listing = _json_object(
            state.client.list_services(
                job_id=run_id,
                node=owner_node,
                passing_only=True,
                page_size=200,
            )
        )
        items = listing.get("items") or listing.get("services") or []
        if not isinstance(items, list):
            return None
        summary = _json_object(state.client.get_system_summary())
    except Exception:
        return None
    nodes = {
        str(node.get("name") or ""): str(node.get("address") or node.get("grpc_host") or "").strip()
        for node in summary.get("nodes", [])
        if isinstance(node, dict)
    }
    services = [
        service
        for service in items
        if isinstance(service, dict)
        and service.get("job_id") in (None, run_id)
        and str(service.get("status") or "").lower() == "passing"
    ]
    web = [service for service in services if "web_ui" in service.get("tags", [])]
    if len(web) != 1:
        return None
    service = web[0]
    node = str(service.get("node") or "")
    host = nodes.get(node, "")
    try:
        port = int(service.get("port"))
    except (TypeError, ValueError):
        return None
    if not host or host in {"0.0.0.0", "::"} or any(char in host for char in "/?#") or not 1 <= port <= 65535:
        return None
    http_ports = {port}
    websocket_ports = set()
    for candidate in services:
        if candidate.get("node") != node:
            continue
        try:
            candidate_port = int(candidate.get("port"))
        except (TypeError, ValueError):
            continue
        if not 1 <= candidate_port <= 65535:
            continue
        tags = set(candidate.get("tags") or [])
        if tags & {"web_ui", "web_ui_proxy", "video"}:
            http_ports.add(candidate_port)
        if "websocket" in tags:
            websocket_ports.add(candidate_port)
    title = str((service.get("meta") or {}).get("title") or service.get("name") or "Job Web UI")[:200]
    metadata = {
        "job_id": job_id,
        "execution_id": run_id,
        "service_name": service.get("name"),
        "node_id": node,
        "proxy": {
            "schema_version": "mn.web_ui.proxy.v1",
            "http_ports": sorted(http_ports),
            "websocket_ports": sorted(websocket_ports),
        },
    }
    service_meta = service.get("meta")
    if isinstance(service_meta, dict) and "load_event" in service_meta:
        metadata["load_event"] = service_meta["load_event"]
    return {
        "job_id": job_id,
        "ui": {
            "schema_version": "mn.web_ui.external.v1",
            "renderer": "external-url",
            "job_id": job_id,
            "title": title,
            "metadata": metadata,
        },
        "web_ui": {
            "kind": "service",
            "adapter": "external-url",
            "status": "running",
            "title": title,
            "url": f"http://{host}:{port}",
            "job_id": job_id,
            "metadata": metadata,
        },
    }


def _job_ui_handle_from_directory(candidate: Path | None, job_id: str) -> dict[str, Any] | None:
    if candidate is None:
        return None
    try:
        if not candidate.is_dir():
            return None
    except OSError:
        return None
    ui = _read_json_file(candidate / "ui.json")
    web_ui = _read_json_file(candidate / "web_ui.json")
    if not ui or not web_ui:
        return None
    if ui.get("job_id") != job_id or web_ui.get("job_id") != job_id:
        return None
    return {"job_id": job_id, "ui": ui, "web_ui": web_ui}


def _owner_node_job_ui_handle(job_id: str) -> dict[str, Any] | None:
    """Load a missing UI handle from the job's federated owner node."""

    try:
        raw_job = state.client.get_job(job_id)
        job = _json_object(raw_job)
        nested_job = job.get("job") if isinstance(job.get("job"), dict) else {}
        owner_node = _first_string(job.get("owner_node"), nested_job.get("owner_node"))
        if not owner_node:
            return None
        summary = _json_object(state.client.get_system_summary())
        nodes = summary.get("nodes") if isinstance(summary.get("nodes"), list) else []
        owner = next(
            (
                node
                for node in nodes
                if isinstance(node, dict)
                and _first_string(node.get("name"), node.get("node_name"), node.get("id")) == owner_node
            ),
            None,
        )
        if not isinstance(owner, dict) or owner.get("self?") is True or owner.get("self") is True:
            return None
        host = _first_string(owner.get("address"), owner.get("grpc_host"), owner.get("hostname"))
        if not _valid_owner_api_host(host):
            return None
        config = state.refresh_config_from_env()
        from mn_api.job_ui_routing import node_api_base_url

        base_url = node_api_base_url(owner)
        headers = {}
        token = str(getattr(config, "api_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{base_url}/jobs/{urllib.parse.quote(job_id, safe='-._')}/ui?check_readiness=false",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=3.0) as response:
            remote = _json_object(response.read())
    except HTTPException:
        raise
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    except Exception:
        return None

    ui = remote.get("ui") if isinstance(remote, dict) else None
    web_ui = remote.get("web_ui") if isinstance(remote, dict) else None
    if not isinstance(ui, dict) or not isinstance(web_ui, dict):
        return None
    if ui.get("job_id") != job_id or web_ui.get("job_id") != job_id:
        return None
    return {"job_id": job_id, "ui": ui, "web_ui": web_ui}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _valid_owner_api_host(value: str) -> bool:
    """Accept a registered node host, but never a URL or path fragment."""

    host = str(value or "").strip()
    if not host or "/" in host or "@" in host or any(char.isspace() for char in host):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", host))
