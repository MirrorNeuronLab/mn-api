from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from mn_api.errors import handle_grpc_error

INTERFACE_VERSION = 2


def client_json_response(
    call: Callable[[], str | bytes | bytearray],
    *,
    preserve_http_exceptions: bool = False,
    add_version: bool = True,
) -> Any:
    try:
        decoded = json.loads(call())
        if isinstance(decoded, dict) and add_version:
            decoded["version"] = INTERFACE_VERSION
        if not add_version:
            return JSONResponse(content=decoded)
        return decoded
    except HTTPException as exc:
        if preserve_http_exceptions:
            raise
        return handle_grpc_error(exc)
    except Exception as exc:
        return handle_grpc_error(exc)
