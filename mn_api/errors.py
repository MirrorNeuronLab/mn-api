from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mn_api import state
from mn_sdk.errors import AppError, normalize_exception, sanitize_context

def problem_response(
    *,
    status_code: int,
    error: str,
    title: str,
    detail: str,
    validation: dict | None = None,
    extra: dict | None = None,
    instance: str | None = None,
    request_id: str | None = None,
    headers: Mapping[str, str] | None = None,
):
    content = {
        "type": f"https://mirrorneuron.io/problems/{error}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance or "",
        "code": error,
        "request_id": request_id or "",
    }
    if validation is not None:
        content["errors"] = (validation.get("issues") or [
            {"code": error, "message": message, "severity": "error"}
            for message in validation.get("errors", [])
        ])[:100]
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type="application/problem+json",
        headers=dict(headers or {}),
    )


def validation_problem_response(
    validation: dict,
    *,
    status_code: int = 422,
    error: str = "input_validation_failed",
    title: str = "Input validation failed",
    detail: str = "One or more input fields failed validation.",
    extra: dict | None = None,
):
    return problem_response(
        status_code=status_code,
        error=error,
        title=title,
        detail=detail,
        validation=validation,
        extra=extra,
    )


def app_error_response(app_error: AppError, *, request: Request | None = None, context: Mapping[str, Any] | None = None):
    request_id = _request_id(request)
    extra = {"hint": app_error.hint} if app_error.hint else {}
    return problem_response(
        status_code=app_error.http_status,
        error=app_error.code,
        title=_title_from_code(app_error.code),
        detail=app_error.user_message,
        extra=extra,
        instance=request.url.path if request is not None else None,
        request_id=request_id,
    )


def handle_grpc_error(error: Exception):
    app_error = normalize_exception(error)
    _log_exception(error, app_error, {"handler": "handle_grpc_error"})
    return app_error_response(app_error)


async def app_error_exception_handler(request: Request, exc: AppError):
    _log_exception(exc.cause or exc, exc, _request_context(request))
    return app_error_response(exc, request=request)


async def http_exception_handler(request: Request, exc: HTTPException):
    code, title = _http_problem_identity(exc.status_code)
    detail = exc.detail if isinstance(exc.detail, str) else title
    return problem_response(
        status_code=exc.status_code,
        error=code,
        title=title,
        detail=detail,
        instance=request.url.path,
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for issue in exc.errors()[:100]:
        location = [str(part) for part in issue.get("loc", ()) if part not in {"body", "query", "path"}]
        errors.append(
            {
                "field": ".".join(location),
                "code": str(issue.get("type") or "invalid"),
                "message": str(issue.get("msg") or "Invalid value."),
            }
        )
    return problem_response(
        status_code=422,
        error="validation_failed",
        title="Request validation failed",
        detail="One or more request fields are invalid.",
        validation={"issues": errors},
        instance=request.url.path,
        request_id=_request_id(request),
    )


async def unexpected_exception_handler(request: Request, exc: Exception):
    app_error = normalize_exception(exc)
    _log_exception(exc, app_error, _request_context(request))
    return app_error_response(app_error, request=request)


def _title_from_code(code: str) -> str:
    return code.removeprefix("MN_").replace("_", " ").title()


def _request_id(request: Request | None) -> str:
    if request is None:
        return ""
    return str(
        getattr(request.state, "request_id", "")
        or request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or ""
    )


def _http_problem_identity(status_code: int) -> tuple[str, str]:
    return {
        400: ("bad_request", "Bad request"),
        401: ("authentication_required", "Authentication required"),
        403: ("permission_denied", "Permission denied"),
        404: ("not_found", "Resource not found"),
        405: ("method_not_allowed", "Method not allowed"),
        409: ("conflict", "Request conflict"),
        412: ("precondition_failed", "Precondition failed"),
        413: ("request_too_large", "Request too large"),
        422: ("validation_failed", "Request validation failed"),
        428: ("precondition_required", "Precondition required"),
        429: ("rate_limited", "Too many requests"),
        502: ("upstream_failure", "Upstream service failed"),
        503: ("service_unavailable", "Service unavailable"),
        500: ("internal_error", "Internal server error"),
    }.get(status_code, ("request_failed", "Request failed"))


def _request_context(request: Request) -> dict[str, Any]:
    return sanitize_context(
        {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "request_id": _request_id(request),
        }
    )


def _log_exception(error: Exception, app_error: AppError, context: Mapping[str, Any] | None = None) -> None:
    sanitized = sanitize_context(context)
    exc_info = (type(error), error, error.__traceback__)
    state.logger.error(
        "API request failed error_code=%s sanitized_context=%s",
        app_error.code,
        sanitized,
        exc_info=exc_info,
    )
