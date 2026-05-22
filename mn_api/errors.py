from __future__ import annotations

from fastapi.responses import JSONResponse
import json
import grpc

from mn_api import state


def problem_response(
    *,
    status_code: int,
    error: str,
    title: str,
    detail: str,
    validation: dict | None = None,
    extra: dict | None = None,
):
    content = {
        "type": f"https://mirrorneuron.local/problems/{error}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "error": error,
    }
    if validation is not None:
        content["validation"] = validation
        content["errors"] = validation.get("issues") or [
            {"code": error, "message": message, "severity": "error"}
            for message in validation.get("errors", [])
        ]
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type="application/problem+json",
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


def handle_grpc_error(error: Exception):
    state.logger.exception("Request failed")
    if isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return JSONResponse(
            status_code=503,
            content={"error": "resource_overloaded", "detail": error.details()},
        )
    if isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.FAILED_PRECONDITION:
        detail = error.details()
        error_name = "requirements_not_met" if str(detail).startswith("requirements_not_met:") else "failed_precondition"
        validation = _validation_report_from_prefixed_detail(str(detail), "requirements_not_met:")
        return problem_response(
            status_code=412,
            error=error_name,
            title="Runtime requirements not met",
            detail=_human_detail(str(detail), "requirements_not_met:"),
            validation=validation,
        )
    if isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.INVALID_ARGUMENT:
        detail = error.details()
        if str(detail).startswith("input_validation_failed:"):
            validation = _validation_report_from_prefixed_detail(str(detail), "input_validation_failed:")
            return validation_problem_response(
                validation or _legacy_report(str(detail), "input_validation_failed:"),
                detail=_human_detail(str(detail), "input_validation_failed:"),
            )
        return problem_response(
            status_code=422,
            error="invalid_argument",
            title="Invalid request",
            detail=str(detail),
        )

    if hasattr(error, "details"):
        return JSONResponse(status_code=500, content={"error": error.details()})
    return JSONResponse(status_code=500, content={"error": str(error)})


def _validation_report_from_prefixed_detail(detail: str, prefix: str) -> dict | None:
    if not detail.startswith(prefix):
        return None
    payload = detail[len(prefix):].strip()
    if not payload.startswith("{"):
        return _legacy_report(detail, prefix)
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else _legacy_report(detail, prefix)


def _legacy_report(detail: str, prefix: str) -> dict:
    message = _human_detail(detail, prefix)
    return {
        "version": "validation.report/v1",
        "ok": False,
        "status": "failed",
        "error_count": 1,
        "errors": [message],
        "issues": [
            {
                "code": prefix.rstrip(":") or "validation_failed",
                "message": message,
                "help": "Review the validation details and retry.",
                "severity": "error",
            }
        ],
        "results": [],
    }


def _human_detail(detail: str, prefix: str) -> str:
    if detail.startswith(prefix):
        stripped = detail[len(prefix):].strip()
        if stripped.startswith("{"):
            report = _validation_report_from_prefixed_detail(detail, prefix)
            if report and report.get("errors"):
                return "; ".join(str(error) for error in report["errors"])
        return stripped
    return detail
