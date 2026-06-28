from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from mn_api.errors import handle_grpc_error

INTERFACE_VERSION = 1


def client_json_response(
    call: Callable[[], str | bytes | bytearray],
    *,
    preserve_http_exceptions: bool = False,
) -> Any:
    try:
        decoded = json.loads(call())
        if isinstance(decoded, dict):
            decoded.setdefault("version", INTERFACE_VERSION)
        return decoded
    except HTTPException as exc:
        if preserve_http_exceptions:
            raise
        return handle_grpc_error(exc)
    except Exception as exc:
        return handle_grpc_error(exc)
