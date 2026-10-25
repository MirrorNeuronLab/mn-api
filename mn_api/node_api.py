"""Resolve only Core-registered native HTTP endpoints."""

from urllib.parse import urlsplit

from fastapi import HTTPException


def registered_host(node):
    if not isinstance(node, dict):
        raise HTTPException(503, "The registered computer has no valid address.")
    host = str(node.get("address") or node.get("grpc_host") or "")
    try:
        parsed = urlsplit("http://" + (f"[{host}]" if ":" in host else host))
        valid = (parsed.hostname and parsed.hostname == host and not parsed.username
                 and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or host in {"0.0.0.0", "::"}:
        raise HTTPException(503, "The registered computer has no valid address.")
    return host


def node_api_base_url(node):
    host = registered_host(node)
    hardware = node.get("hardware")
    endpoint = hardware.get("http_api") if isinstance(hardware, dict) else None
    if (not isinstance(endpoint, dict) or endpoint.get("schema_version") != "mn.http_api.endpoint.v1"
            or endpoint.get("enabled") is not True or endpoint.get("scheme") not in {"http", "https"}
            or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535):
        raise HTTPException(503, "The computer has no registered API endpoint. Update its runtime.")
    authority = '[' + host + ']' if ':' in host else host
    return f"{endpoint['scheme']}://{authority}:{endpoint['port']}/api/v1"
