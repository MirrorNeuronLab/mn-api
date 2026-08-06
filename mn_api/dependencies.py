from __future__ import annotations

from fastapi import Header, HTTPException, Request, WebSocket, WebSocketException
from fastapi.responses import JSONResponse

from mn_api import state
from mn_api.config import auth_enabled

INTERFACE_VERSION = 2


async def enforce_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    try:
        request_size = int(content_length) if content_length else 0
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"version": INTERFACE_VERSION, "error": "invalid_content_length"},
        )

    if request_size > state.config.request_size_limit_bytes:
        return JSONResponse(
            status_code=413,
            content={
                "version": INTERFACE_VERSION,
                "error": "request_too_large",
                "limit_bytes": state.config.request_size_limit_bytes,
            },
        )
    return await call_next(request)


def require_auth(authorization: str = Header(default="")):
    if not auth_enabled(state.config):
        return None

    expected = f"Bearer {state.config.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return None


async def require_websocket_auth(websocket: WebSocket) -> None:
    if not auth_enabled(state.config):
        return

    expected_token = str(state.config.api_token or "")
    authorization = str(websocket.headers.get("authorization") or "")
    query_token = str(websocket.query_params.get("token") or "")
    if authorization == f"Bearer {expected_token}" or query_token == expected_token:
        return
    raise WebSocketException(code=1008, reason="missing or invalid bearer token")
