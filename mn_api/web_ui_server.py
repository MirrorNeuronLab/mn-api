from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse


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
    configured = os.getenv("MN_WEB_UI_API_BASE_URL") or os.getenv("MN_API_BASE_URL")
    if configured:
        return configured.rstrip("/")
    host = os.getenv("MN_API_HOST", "localhost")
    port = os.getenv("MN_API_PORT", "54001")
    return f"http://{host}:{port}/api/v1"


def create_app(dist_dir: str | Path | None = None, api_url: str | None = None) -> FastAPI:
    resolved_dist = resolve_dist_dir(str(dist_dir) if dist_dir is not None else os.getenv("MN_WEB_UI_DIST_DIR"))
    index_file = resolved_dist / "index.html"
    upstream_api = (api_url or api_base_url()).rstrip("/")

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
    target_url = _target_url(path, request.url.query, upstream_api)
    proxy_headers = _proxy_headers(request.headers.items())
    upstream_request = urllib.request.Request(target_url, data=body or None, headers=proxy_headers, method=request.method)

    try:
        upstream_response = urllib.request.urlopen(upstream_request, timeout=float(os.getenv("MN_WEB_UI_PROXY_TIMEOUT_SECONDS", "30")))
    except urllib.error.HTTPError as exc:
        return Response(
            content=exc.read(),
            status_code=exc.code,
            headers=_response_headers(exc.headers.items()),
        )
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "component": "web-ui-proxy", "detail": str(exc), "target": target_url},
            status_code=502,
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
    host = os.getenv("MN_WEB_UI_HOST", "localhost")
    port = int(os.getenv("MN_WEB_UI_PORT", "55173"))
    uvicorn.run("mn_api.web_ui_server:create_app", host=host, port=port, factory=True, reload=False)


def _target_url(path: str, query: str, upstream_api: str) -> str:
    base = upstream_api.rstrip("/")
    base_without_version = base[:-3] if base.endswith("/v1") else base
    encoded_path = urllib.parse.quote(path, safe="/:@")
    target = f"{base_without_version}/{encoded_path}"
    return f"{target}?{query}" if query else target


def _proxy_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    excluded = HOP_BY_HOP_HEADERS | {"host"}
    return {key: value for key, value in headers if key.lower() not in excluded}


def _response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    excluded = HOP_BY_HOP_HEADERS | {"content-length", "date", "server"}
    return {key: value for key, value in headers if key.lower() not in excluded}


def _stream_response(upstream_response) -> Iterable[bytes]:
    try:
        while True:
            chunk = upstream_response.read(8192)
            if not chunk:
                break
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
