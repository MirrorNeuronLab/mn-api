from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
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
    if "text/event-stream" in content_type.lower():
        return StreamingResponse(
            _stream_response(upstream_response),
            status_code=status_code,
            headers=headers,
            media_type=content_type,
        )

    try:
        payload = upstream_response.read()
    finally:
        upstream_response.close()
    return Response(content=payload, status_code=status_code, headers=headers)


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
    return {key: value for key, value in headers if key.lower() not in excluded}


def _stream_response(upstream_response) -> Iterable[bytes]:
    try:
        while True:
            line = upstream_response.readline()
            if not line:
                break
            yield line
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
