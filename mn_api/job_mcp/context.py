"""Per-request job identity and runtime reply errors."""

from __future__ import annotations

import contextvars
from typing import Any

import grpc

_current_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_mcp_job_id", default="")


class JobMCPNotFoundError(RuntimeError):
    pass


def _reply_rpc(command):
    try:
        return command()
    except JobMCPUnavailableError:
        return {"schema_version": "mn.mcp.job_reply_observation.v1", "state": "unavailable"}
    except grpc.RpcError as error:
        if error.code() not in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
            raise
        # A relay timeout says nothing about the owner claim's outcome. Keep
        # transport unavailability distinct from an owner-reported failure.
        return {"schema_version": "mn.mcp.job_reply_observation.v1", "state": "unavailable"}


class JobMCPUnavailableError(RuntimeError):
    pass


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""
