from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from mn_api.config import WebUiConfig


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class JobUiProxyError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def resolve_dist_dir(value: str | None = None, cwd: Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()

    root = (cwd or Path.cwd()).resolve()
    nested = root / "dist"
    if (nested / "index.html").exists():
        return nested
    return root


def api_base_url() -> str:
    return WebUiConfig.from_env().api_base_url


def create_app(dist_dir: str | Path | None = None, api_url: str | None = None) -> FastAPI:
    config = WebUiConfig.from_env()
    configured_dist = dist_dir if dist_dir is not None else config.dist_dir
    resolved_dist = resolve_dist_dir(str(configured_dist) if configured_dist is not None else None)
    index_file = resolved_dist / "index.html"
    upstream_api = (api_url or config.api_base_url).rstrip("/")

    app = FastAPI(title="MirrorNeuron Web UI", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok" if index_file.exists() else "missing",
            "component": "web-ui",
            "api_base_url": upstream_api,
            "dist_dir": str(resolved_dist),
        }

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_api(path: str, request: Request):
        return await proxy_request(path, request, upstream_api)

    @app.api_route(
        "/job-ui-proxy/{job_id}/{port}",
        methods=["GET", "HEAD"],
    )
    @app.api_route(
        "/job-ui-proxy/{job_id}/{port}/{path:path}",
        methods=["GET", "HEAD"],
    )
    def proxy_job_ui(job_id: str, port: int, request: Request, path: str = ""):
        return proxy_job_ui_request(
            job_id=job_id,
            port=port,
            path=path,
            query=request.url.query,
            method=request.method,
            request_headers=request.headers.items(),
            upstream_api=upstream_api,
            api_token=config.api_token,
        )

    @app.websocket("/job-ui-proxy/{job_id}/{port}/ws")
    @app.websocket("/job-ui-proxy/{job_id}/{port}/ws/{path:path}")
    async def proxy_job_ui_websocket(websocket: WebSocket, job_id: str, port: int, path: str = ""):
        await proxy_job_ui_websocket_request(
            websocket=websocket,
            job_id=job_id,
            port=port,
            path=path,
            query=websocket.url.query,
            upstream_api=upstream_api,
            api_token=config.api_token,
        )

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    def serve_web_ui(path: str):
        if not index_file.exists():
            raise HTTPException(status_code=503, detail=f"Web UI build not found at {index_file}")

        requested = (resolved_dist / (path or "index.html")).resolve()
        if not _is_relative_to(requested, resolved_dist):
            raise HTTPException(status_code=404, detail="Not found")
        if requested.is_file():
            return FileResponse(requested)
        return FileResponse(index_file)

    return app


async def proxy_request(path: str, request: Request, upstream_api: str) -> Response:
    body = await request.body()
    config = WebUiConfig.from_env()
    target_url = _target_url(path, request.url.query, upstream_api)
    proxy_headers = _proxy_headers(request.headers.items(), api_token=config.api_token)
    upstream_request = urllib.request.Request(
        target_url,
        data=body or None,
        headers=proxy_headers,
        method=request.method,
    )

    try:
        upstream_response = urllib.request.urlopen(
            upstream_request,
            timeout=WebUiConfig.from_env().proxy_timeout_seconds,
        )
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=exc.code,
            headers=_response_headers(exc.headers.items()),
        )
    except Exception:
        return JSONResponse(
            {
                "type": "https://mirrorneuron.io/problems/upstream_failure",
                "title": "Upstream service failed",
                "status": 502,
                "detail": "The MirrorNeuron API could not be reached.",
                "instance": request.url.path,
                "code": "upstream_failure",
                "request_id": request.headers.get("x-request-id", ""),
                "component": "web-ui-proxy",
                "target": target_url,
            },
            status_code=502,
            media_type="application/problem+json",
        )

    status_code = int(getattr(upstream_response, "status", upstream_response.getcode()))
    headers = _response_headers(upstream_response.headers.items())
    content_type = headers.get("content-type", "")
    if _is_streaming_content_type(content_type):
        return StreamingResponse(
            _stream_response(upstream_response)
            if "text/event-stream" in content_type.lower()
            else _stream_binary_response(upstream_response),
            status_code=status_code,
            headers=headers,
            media_type=content_type,
        )

    try:
        payload = upstream_response.read()
    finally:
        upstream_response.close()
    return Response(content=payload, status_code=status_code, headers=headers)


def proxy_job_ui_request(
    *,
    job_id: str,
    port: int,
    path: str,
    query: str,
    method: str,
    request_headers: Iterable[tuple[str, str]],
    upstream_api: str,
    api_token: str,
) -> Response:
    """Forward a declared job UI through the local Web UI service.

    The target host originates exclusively from the authenticated job handle.
    ``port`` is checked against the handle's explicit proxy policy, so this is
    not a general-purpose LAN proxy.
    """

    try:
        web_ui = _load_job_web_ui(job_id, upstream_api=upstream_api, api_token=api_token)
        target_url = _job_ui_target_url(web_ui, port=port, path=path, query=query)
    except JobUiProxyError as exc:
        return _job_ui_proxy_problem(exc.status_code, exc.detail)

    upstream_request = urllib.request.Request(
        target_url,
        headers=_remote_proxy_headers(request_headers),
        method=method,
    )
    try:
        upstream_response = urllib.request.urlopen(
            upstream_request,
            timeout=WebUiConfig.from_env().proxy_timeout_seconds,
        )
    except urllib.error.HTTPError as exc:
        return _job_ui_proxy_problem(exc.code, "The job Web UI did not accept that request.")
    except Exception:
        return _job_ui_proxy_problem(502, "The job Web UI could not be reached.")

    status_code = int(getattr(upstream_response, "status", upstream_response.getcode()))
    headers = _remote_response_headers(upstream_response.headers.items())
    content_type = headers.get("content-type", "")
    if _is_streaming_content_type(content_type):
        return StreamingResponse(
            _stream_response(upstream_response)
            if "text/event-stream" in content_type.lower()
            else _stream_binary_response(upstream_response),
            status_code=status_code,
            headers=headers,
            media_type=content_type,
        )

    try:
        payload = upstream_response.read()
    finally:
        upstream_response.close()
    if "text/html" in content_type.lower():
        payload = _inject_job_ui_proxy_config(payload, job_id)
    return Response(content=payload, status_code=status_code, headers=headers)


async def proxy_job_ui_websocket_request(
    *,
    websocket: WebSocket,
    job_id: str,
    port: int,
    path: str,
    query: str,
    upstream_api: str,
    api_token: str,
) -> None:
    """Bridge an explicitly declared job WebSocket without exposing Spark."""

    try:
        web_ui = await asyncio.to_thread(
            _load_job_web_ui,
            job_id,
            upstream_api=upstream_api,
            api_token=api_token,
        )
        target_url = _job_ui_target_url(web_ui, port=port, path=path, query=query, websocket=True)
    except JobUiProxyError:
        await _close_websocket(websocket, 1008)
        return

    try:
        import websockets

        async with websockets.connect(
            target_url,
            open_timeout=WebUiConfig.from_env().proxy_timeout_seconds,
            max_size=None,
        ) as upstream:
            await websocket.accept()
            await _bridge_websockets(websocket, upstream)
    except Exception:
        await _close_websocket(websocket, 1011)


def _load_job_web_ui(job_id: str, *, upstream_api: str, api_token: str) -> dict[str, Any]:
    target_url = _target_url(f"jobs/{job_id}/ui", "", upstream_api)
    request = urllib.request.Request(
        target_url,
        headers=_proxy_headers((), api_token=api_token),
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=WebUiConfig.from_env().proxy_timeout_seconds)
    except urllib.error.HTTPError as exc:
        status_code = 404 if exc.code == 404 else 502
        raise JobUiProxyError(status_code, "No Web UI is registered for this job.") from exc
    except Exception as exc:
        raise JobUiProxyError(502, "The MirrorNeuron API could not be reached.") from exc

    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobUiProxyError(502, "The job Web UI handle is invalid.") from exc
    finally:
        response.close()
    web_ui = payload.get("web_ui") if isinstance(payload, dict) else None
    if not isinstance(web_ui, dict):
        raise JobUiProxyError(404, "No Web UI is registered for this job.")
    return web_ui


def _job_ui_target_url(
    web_ui: dict[str, Any],
    *,
    port: int,
    path: str,
    query: str,
    websocket: bool = False,
) -> str:
    raw_url = web_ui.get("url")
    parsed = urllib.parse.urlsplit(str(raw_url or ""))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise JobUiProxyError(404, "The job Web UI endpoint is invalid.")

    try:
        primary_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise JobUiProxyError(404, "The job Web UI endpoint is invalid.") from exc
    allowed_ports = _allowed_job_ui_ports(web_ui, websocket=websocket, primary_port=primary_port)
    if port not in allowed_ports:
        raise JobUiProxyError(404, "That job Web UI connection is not declared.")

    path = _safe_proxy_path(path)
    scheme = (
        "wss"
        if websocket and parsed.scheme == "https"
        else "ws"
        if websocket
        else parsed.scheme
    )
    host = parsed.hostname
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    target = f"{scheme}://{display_host}:{port}/{path}" if path else f"{scheme}://{display_host}:{port}/"
    return f"{target}?{query}" if query else target


def _allowed_job_ui_ports(web_ui: dict[str, Any], *, websocket: bool, primary_port: int) -> set[int]:
    metadata = web_ui.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    policy = metadata.get("proxy")
    policy = policy if isinstance(policy, dict) else {}
    key = "websocket_ports" if websocket else "http_ports"
    ports = {_valid_port(item) for item in policy.get(key, []) if _valid_port(item) is not None}
    if not websocket:
        ports.add(primary_port)
    return {port for port in ports if port is not None}


def _valid_port(value: Any) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _safe_proxy_path(path: str) -> str:
    parts = [part for part in str(path or "").split("/") if part]
    if any(urllib.parse.unquote(part) in {".", ".."} for part in parts):
        raise JobUiProxyError(404, "That job Web UI path is invalid.")
    return "/".join(urllib.parse.quote(urllib.parse.unquote(part), safe=":@") for part in parts)


def _remote_proxy_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    excluded = HOP_BY_HOP_HEADERS | {"host", "authorization", "cookie", "accept-encoding", "origin"}
    return {key: value for key, value in headers if key.lower() not in excluded}


def _remote_response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    excluded = HOP_BY_HOP_HEADERS | {"content-length", "date", "server", "location", "set-cookie"}
    return {key.lower(): value for key, value in headers if key.lower() not in excluded}


def _inject_job_ui_proxy_config(payload: bytes, job_id: str) -> bytes:
    encoded_job_id = urllib.parse.quote(job_id, safe="-._")
    proxy_base = f"/job-ui-proxy/{encoded_job_id}"
    script = f"<script>window.__MN_JOB_UI_PROXY_BASE__={json.dumps(proxy_base)};</script>".encode("ascii")
    closing_head = payload.lower().rfind(b"</head>")
    return payload[:closing_head] + script + payload[closing_head:] if closing_head >= 0 else script + payload


def _job_ui_proxy_problem(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


async def _bridge_websockets(browser: WebSocket, upstream: Any) -> None:
    async def browser_to_upstream() -> None:
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_browser() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await browser.send_bytes(message)
            else:
                await browser.send_text(message)

    pending: set[asyncio.Task[Any]] = set()
    try:
        _completed, pending = await asyncio.wait(
            {
                asyncio.create_task(browser_to_upstream()),
                asyncio.create_task(upstream_to_browser()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _close_websocket(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        return


def start() -> None:
    config = WebUiConfig.from_env()
    uvicorn.run("mn_api.web_ui_server:create_app", host=config.host, port=config.port, factory=True, reload=False)


def _target_url(path: str, query: str, upstream_api: str) -> str:
    base = upstream_api.rstrip("/")
    normalized_path = path[3:] if base.endswith("/v1") and path.startswith("v1/") else path
    encoded_path = urllib.parse.quote(normalized_path, safe="/:@")
    target = f"{base}/{encoded_path}"
    return f"{target}?{query}" if query else target


def _proxy_headers(headers: Iterable[tuple[str, str]], *, api_token: str = "") -> dict[str, str]:
    excluded = HOP_BY_HOP_HEADERS | {"host"}
    forwarded = {key: value for key, value in headers if key.lower() not in excluded}
    has_authorization = any(key.lower() == "authorization" for key in forwarded)
    if api_token and not has_authorization:
        forwarded["Authorization"] = f"Bearer {api_token}"
    return forwarded


def _response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    excluded = HOP_BY_HOP_HEADERS | {"content-length", "date", "server"}
    return {key.lower(): value for key, value in headers if key.lower() not in excluded}


def _is_streaming_content_type(content_type: str) -> bool:
    normalized = str(content_type or "").lower()
    return "text/event-stream" in normalized or "multipart/" in normalized


def _stream_response(upstream_response) -> Iterable[bytes]:
    try:
        while True:
            line = upstream_response.readline()
            if not line:
                break
            yield line
    finally:
        upstream_response.close()


def _stream_binary_response(upstream_response) -> Iterable[bytes]:
    try:
        while chunk := upstream_response.read(64 * 1024):
            yield chunk
    finally:
        upstream_response.close()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    start()
