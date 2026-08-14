from __future__ import annotations

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mn_api import state
from mn_api.config import auth_enabled
from mn_api.errors import problem_response


async def enforce_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    try:
        request_size = int(content_length) if content_length else 0
    except ValueError:
        return problem_response(
            status_code=400,
            error="invalid_content_length",
            title="Invalid Content-Length",
            detail="Content-Length must be an integer.",
            instance=request.url.path,
            request_id=str(getattr(request.state, "request_id", "")),
        )

    if request_size > state.config.request_size_limit_bytes:
        return problem_response(
            status_code=413,
            error="request_too_large",
            title="Request too large",
            detail=f"The request exceeds the {state.config.request_size_limit_bytes} byte limit.",
            instance=request.url.path,
            request_id=str(getattr(request.state, "request_id", "")),
        )
    return await call_next(request)


_bearer = HTTPBearer(auto_error=False)


def require_auth(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)) -> str:
    if not auth_enabled(state.config):
        return "anonymous"

    if credentials is None or credentials.scheme.lower() != "bearer" or credentials.credentials != state.config.api_token:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return "authenticated"
