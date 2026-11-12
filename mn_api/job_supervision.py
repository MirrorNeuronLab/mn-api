"""MCP protocol adaptation over runtime-owned durable supervision."""

from contextvars import ContextVar

import anyio
from mcp.shared.exceptions import MCPError
from mn_sdk.runtime_control import RuntimeControlClient
from mn_api.runtime_mcp import wire_value

native_launch_authority = ContextVar("mcp_native_launch_authority", default=False)


class BoundJobSupervision:
    def __init__(self, context_provider, bound_job_id):
        self.context_provider = context_provider
        self.bound_job_id = bound_job_id

    def _call(self, method, **arguments):
        from mn_sdk.errors import AppError

        try:
            with RuntimeControlClient(timeout=120) as client:
                return client.command(
                    "mcp.supervision",
                    {
                        "job_id": self.bound_job_id(),
                        "method": method,
                        "internal": native_launch_authority.get(),
                        **wire_value(arguments),
                    },
                )
        except AppError as exc:
            if exc.code == "MN_RUNTIME_UNAVAILABLE":
                raise MCPError(
                    -32000,
                    "The runtime connection is temporarily unavailable.",
                    data={"code": "MN_RUNTIME_UNAVAILABLE", "retryable": True},
                ) from exc
            if exc.http_status == 404:
                raise KeyError(exc.user_message) from exc
            if exc.http_status == 422:
                raise ValueError(exc.user_message) from exc
            raise

    def identity(self):
        return self._call("identity")

    def declaration(self):
        return self._call("declaration")

    async def capabilities(self):
        return await anyio.to_thread.run_sync(lambda: self._call("capabilities"))

    async def invoke(self, name, arguments, ctx):
        if name == "watch_job_activity" and ctx is not None:
            from mn_sdk_mcp import resolve_job_activity_receipt

            receipt = resolve_job_activity_receipt(ctx)
            if receipt is not None:
                return receipt
        if name == "get_job_context" and ctx is not None and getattr(self, "resolve_human_input", None):
            pending = self.resolve_human_input(ctx)
            if pending is not None:
                return pending
        metadata = {}
        if name == "start_run" and ctx is not None:
            meta = ctx.request_context.meta
            meta = meta.model_dump(by_alias=True) if hasattr(meta, "model_dump") else dict(meta or {})
            # Pass only the declared ephemeral launch input, never the MCP context.
            if "mn.launch" in meta:
                metadata["mn.launch"] = meta["mn.launch"]
        result = await anyio.to_thread.run_sync(
            lambda: self._call("invoke", name=name, arguments=arguments, metadata=metadata)
        )
        if isinstance(result, dict) and result.get("result_type") == "input_required":
            from mcp.types import InputRequiredResult

            return InputRequiredResult.model_validate(result)
        return result
