"""The MCP surface owns protocol sessions; the runtime owns job supervision."""

from pydantic import BaseModel

from mn_sdk.errors import AppError
from mn_sdk.runtime_contract.mcp import CONTEXT_METHODS
from mn_sdk.runtime_control import RuntimeControlClient


def wire_value(value):
    if isinstance(value, BaseModel):
        return wire_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire_value(item) for item in value]
    return value


class JobContextProvider:
    def __getattr__(self, name):
        if name not in CONTEXT_METHODS:
            raise AttributeError(name)

        def invoke(job_id, *args, **kwargs):
            try:
                with RuntimeControlClient(timeout=120) as client:
                    return client.command(
                        "mcp.context",
                        {"job_id": job_id, "method": name, "args": wire_value(args), "kwargs": wire_value(kwargs)},
                    )
            except AppError as exc:
                from .job_mcp import JobMCPNotFoundError, JobMCPUnavailableError

                if exc.http_status == 404:
                    raise JobMCPNotFoundError("The requested co-worker is unavailable.") from exc
                if exc.http_status in {502, 503, 504}:
                    raise JobMCPUnavailableError(exc.user_message) from exc
                raise

        return invoke
