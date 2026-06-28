from __future__ import annotations

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mn_api import state
from mn_api.config import auth_enabled

INTERFACE_VERSION = 1


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
