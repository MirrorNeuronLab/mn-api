"""Authenticated relay to an owning computer's private, allowlisted Job UI."""

import asyncio
import hmac
import urllib.request
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse

from mn_api import state
from mn_api.config import WebUiConfig, auth_enabled
from mn_api.dependencies import require_auth
from mn_api.job_ui_readiness import PROBE_HEADER
from mn_api.job_ui_routing import EXECUTION_HEADER, HOP_HEADER, placement
from mn_api.routes import jobs
from mn_api.web_ui_server import (
    _job_ui_target_url, _remote_proxy_headers, _remote_response_headers,
    _stream_binary_response, _bridge_websockets, JobUiProxyError,
    _safe_proxy_path,
    _close_websocket,
)
from mn_sdk.runtime_config import RuntimeConfig

router = APIRouter(prefix="/api/v1")


def local_target(job_id, port, path, query, headers, *, websocket=False):
    location = placement(job_id)
    if not location["local"] or headers.get(HOP_HEADER.lower()) != "1":
        raise HTTPException(409, "The Web UI owner changed. Refresh and try again.")
    if headers.get(EXECUTION_HEADER.lower()) != location["execution_id"]:
        raise HTTPException(409, "This Web UI request belongs to an earlier execution.")
    handle = jobs.get_job_ui(job_id, "authenticated", check_readiness=False)
    probe = headers.get(PROBE_HEADER.lower()) == "1"
    if not probe:
        handle = jobs._checked_job_ui_handle(handle)
    web_ui = handle["web_ui"]
    metadata = web_ui.get("metadata") or {}
    if metadata.get("execution_id") != location["execution_id"]:
        raise HTTPException(409, "This Web UI registration belongs to an earlier execution.")
    try:
        # A probe may bypass reachability, never lifecycle, execution or ports.
        _job_ui_target_url(web_ui, port=port, path=path, query=query,
                           websocket=websocket, check_readiness=not probe)
    except JobUiProxyError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    base = RuntimeConfig.from_env().web_ui_url.rstrip("/")
    target = f"{base}/job-ui-proxy/{quote(job_id, safe='-._')}/{port}/" + ("ws/" if websocket else "") + _safe_proxy_path(path)
    if websocket:
        target = ("wss" if target.startswith("https:") else "ws") + target[target.index(":"):]
    return target + ("?" + query if query else "")


def owner_headers(headers):
    forwarded = _remote_proxy_headers(headers.items())
    # The owner proxy must not discover readiness recursively through this relay.
    forwarded[PROBE_HEADER] = "1"
    token = WebUiConfig.from_env().api_token
    if token:
        forwarded["Authorization"] = "Bearer " + token
    return forwarded


@router.get("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui")
@router.head("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="head_job_ui_relay")
def relay_http(job_id: str, port: int, path: str, request: Request, _auth=Depends(require_auth)):
    target = local_target(job_id, port, path, request.url.query, request.headers)
    upstream_request = urllib.request.Request(target, headers=owner_headers(request.headers), method=request.method)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        upstream = opener.open(upstream_request, timeout=WebUiConfig.from_env().proxy_timeout_seconds)
    except OSError as exc:
        raise HTTPException(502, "The job Web UI could not be reached.") from exc
    return StreamingResponse(_stream_binary_response(upstream), status_code=upstream.status,
                             headers=_remote_response_headers(upstream.headers.items()))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


@router.websocket("/jobs/{job_id}/ui-proxy/{port}/ws/{path:path}")
async def relay_websocket(websocket: WebSocket, job_id: str, port: int, path: str):
    if auth_enabled(state.config) and not hmac.compare_digest(
        websocket.headers.get("authorization", ""), "Bearer " + state.config.api_token,
    ):
        await websocket.close(code=1008)
        return
    try:
        target = await asyncio.to_thread(local_target, job_id, port, path, websocket.url.query,
                                         websocket.headers, websocket=True)
        import websockets

        async with websockets.connect(target, additional_headers=owner_headers({}),
                                      open_timeout=WebUiConfig.from_env().proxy_timeout_seconds) as upstream:
            await websocket.accept()
            await _bridge_websockets(websocket, upstream)
    except HTTPException:
        await _close_websocket(websocket, 1008)
    except Exception:
        await _close_websocket(websocket, 1011)
    finally:
        await _close_websocket(websocket, 1000)
