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

    if hasattr(error, "details"):
        return JSONResponse(status_code=500, content={"error": error.details()})
    return JSONResponse(status_code=500, content={"error": str(error)})
