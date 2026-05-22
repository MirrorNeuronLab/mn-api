from __future__ import annotations

from fastapi.responses import JSONResponse
import grpc

from mn_api import state


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
        return JSONResponse(status_code=412, content={"error": error_name, "detail": detail})

    if hasattr(error, "details"):
        return JSONResponse(status_code=500, content={"error": error.details()})
    return JSONResponse(status_code=500, content={"error": str(error)})
