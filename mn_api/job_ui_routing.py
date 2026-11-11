"""Browser routing to the connected surface; runtime owners never expose REST."""

from urllib.parse import urlsplit
from fastapi import HTTPException
from mn_sdk.job_ui_proxy import EXECUTION_HEADER as EXECUTION_HEADER, HOP_HEADER as HOP_HEADER


def relay_target(web_ui, job_id, port, path, query, *, websocket=False):
    """Return an owner relay target after the ordinary port/lifecycle checks."""
    from mn_sdk.job_ui_proxy import _safe_proxy_path

    transport = web_ui.get("transport")
    if not isinstance(transport, dict):
        return None
    if transport.get("schema_version") != "mn.web_ui.transport.v1" or not transport.get("execution_id"):
        raise HTTPException(502, "The job UI transport is invalid.")
    base = str(transport.get("owner_api_url") or "")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/v1"
    ):
        raise HTTPException(502, "The job UI transport is invalid.")
    from urllib.parse import quote

    from mn_sdk.job_ui_proxy import JobUiProxyError

    try:
        suffix = ("ws/" if websocket else "") + _safe_proxy_path(path)
    except JobUiProxyError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    target = f"{base}/jobs/{quote(job_id, safe='-._')}/ui-proxy/{port}/{suffix}"
    if websocket:
        target = ("wss" if parsed.scheme == "https" else "ws") + target[target.index(":") :]
    return target + ("?" + query if query else "")
