from __future__ import annotations

import contextvars


import hmac

import json


from contextlib import asynccontextmanager

from typing import Any, Literal, Mapping

import anyio

import grpc

from mcp.server import MCPServer

from mcp.server.mcpserver import Context

from mcp.server.transport_security import TransportSecuritySettings

from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult

from starlette.requests import Request

from pydantic import BaseModel, ConfigDict, Field


from mn_api import state

from mn_api.runtime_mcp import JobContextProvider

from mn_api.errors import problem_response


_current_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_mcp_job_id", default="")


class AssistanceTask(BaseModel):
    """Desktop-owned intent; operational facts and authorization stay runtime-owned."""

    model_config = ConfigDict(extra="forbid", strict=True)
    goal: Literal["setup", "respond", "diagnose", "review", "schedule", "use_own_data"]
    state: Literal["guiding", "collecting", "review", "saving", "completed", "failed"]
    execution_id: str = Field(max_length=512)
    next_question: str = Field(max_length=500)


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


class JobMCPGuard:
    def __init__(self, base_app, enhanced_app, agent_app, provider: JobContextProvider, stream_app=None) -> None:
        self.base_app = base_app
        self.enhanced_app = enhanced_app
        self.agent_app = agent_app
        self.provider = provider
        self.stream_app = stream_app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.base_app(scope, receive, send)
            return
        request = Request(scope, receive)
        job_id = _first_text(scope.get("path_params", {}).get("job_id"))
        authorization = request.headers.get("authorization", "")
        scheme, _, bearer = authorization.partition(" ")
        expected = str(state.config.api_token or "")
        owner = scheme.lower() == "bearer" and bool(expected) and hmac.compare_digest(bearer, expected)
        authority = "internal" if owner else None
        if not owner:
            from mn_api.mcp_access import ConnectionStore

            authority = (
                await anyio.to_thread.run_sync(lambda: ConnectionStore().authority(job_id, bearer))
                if scheme.lower() == "bearer"
                else None
            )
            if authority is None:
                response = problem_response(
                    status_code=401,
                    error="unauthorized",
                    title="Authentication required",
                    detail="Missing or invalid bearer token.",
                    instance=request.url.path,
                    request_id=str(getattr(request.state, "request_id", "")),
                )
                from mn_api.mcp_oauth import issuer

                policy = await anyio.to_thread.run_sync(lambda: ConnectionStore().policy(job_id))
                server_url = policy["server_url"] or str(request.base_url).rstrip("/") + f"/api/v1/jobs/{job_id}/mcp"
                origin = issuer(server_url).removesuffix("/api/v1/mcp/oauth")
                response.headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/api/v1/jobs/{job_id}/mcp"'
                )
                await response(scope, receive, send)
                return

        try:
            response_mode = await anyio.to_thread.run_sync(self.provider.response_mode, job_id)
        except JobMCPNotFoundError as error:
            response = problem_response(
                status_code=404,
                error="job_mcp_not_found",
                title="Job MCP not found",
                detail=str(error),
                instance=request.url.path,
                request_id=str(getattr(request.state, "request_id", "")),
            )
            await response(scope, receive, send)
            return
        except JobMCPUnavailableError as error:
            response = problem_response(
                status_code=503,
                error="job_mcp_unavailable",
                title="Job MCP unavailable",
                detail=str(error),
                instance=request.url.path,
                request_id=str(getattr(request.state, "request_id", "")),
            )
            await response(scope, receive, send)
            return

        token = _current_job_id.set(job_id)
        from mn_api.job_supervision import native_launch_authority

        authority_token = native_launch_authority.set(authority == "internal")
        try:
            selected_app = {
                "agent": self.agent_app,
                "response": self.enhanced_app,
            }.get(response_mode, self.base_app)
            if response_mode == "response" and self.stream_app is not None and request.method == "POST":
                body = await request.body()
                try:
                    message = json.loads(body)
                    params = message.get("params", {})
                    wants_stream = (
                        message.get("method") == "tools/call"
                        and params.get("name") == "ask_job"
                        and params.get("arguments", {}).get("stream") is True
                    )
                except (ValueError, AttributeError, TypeError):
                    wants_stream = False
                if wants_stream:
                    selected_app = self.stream_app
                original_receive = receive
                delivered = False

                async def replay_body():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()

                receive = replay_body
            await selected_app(scope, receive, send)
        finally:
            _current_job_id.reset(token)
            native_launch_authority.reset(authority_token)


def create_job_mcp(provider: JobContextProvider | None = None) -> tuple[list[MCPServer], Any]:
    context_provider = provider or JobContextProvider()

    from mn_sdk_mcp import create_supervision_server
    from mn_api.job_supervision import BoundJobSupervision

    supervision = BoundJobSupervision(context_provider, lambda: _current_job_id.get())

    def new_server(name: str, instructions: str) -> MCPServer:
        return create_supervision_server(name, supervision, instructions=instructions)

    base_server = new_server(
        "MirrorNeuron job context",
        "Read-only supervisory context for the job bound by the MCP URL.",
    )
    enhanced_server = new_server(
        "MirrorNeuron real-time job response",
        "Read safe context or ask grounded, multi-turn questions about the job bound by the MCP URL.",
    )
    stream_server = new_server(
        "MirrorNeuron real-time job response",
        "Stream visible answer text as progress and return one completed grounded answer.",
    )
    agent_server = new_server(
        "MirrorNeuron bounded job agent",
        "Ask the Job's bounded conversational agent for grounded answers, status, explicit simulation control, or explicit learning.",
    )

    def bound_job_id() -> str:
        job_id = _current_job_id.get()
        if not job_id:
            raise JobMCPNotFoundError("The MCP request is not bound to a job.")
        return job_id

    def resolve_human_input(ctx: Context) -> InputRequiredResult | None:
        job_id = bound_job_id()
        if ctx.request_state:
            try:
                state_payload = json.loads(ctx.request_state)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("The MCP request state was invalid.") from error
            request_id = _first_text(state_payload.get("request_id"))
            if not request_id:
                raise ValueError("The MCP request state was invalid.")
            request_key = f"human-response:{request_id}"
            response = (ctx.input_responses or {}).get(request_key)
            if response is None:
                return None
            action = _first_text(getattr(response, "action", "")).lower()
            content = getattr(response, "content", None)
            if action == "accept" and isinstance(content, Mapping):
                context_provider.record_pending_human_response(
                    job_id,
                    request_id,
                    _first_text(content.get("response")),
                )
            return None
        pending = context_provider.get_pending_human_request(job_id)
        if pending is None:
            return None
        request_key = f"human-response:{pending['request_id']}"
        property_schema: dict[str, Any] = {
            "type": "string",
            "title": "Response",
            "minLength": 1,
            "maxLength": 8_000,
        }
        if pending["options"]:
            property_schema["enum"] = pending["options"]
        return InputRequiredResult(
            input_requests={
                request_key: ElicitRequest(
                    params=ElicitRequestFormParams(
                        message=pending["prompt"],
                        meta={"mn.interaction.id": pending["request_id"]},
                        requested_schema={
                            "type": "object",
                            "properties": {"response": property_schema},
                            "required": ["response"],
                        },
                    )
                )
            },
            request_state=json.dumps(
                {"request_id": pending["request_id"]},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    @stream_server.tool(name="ask_job", structured_output=True)
    @enhanced_server.tool(
        name="ask_job",
        description=(
            "Ask a fast, grounded, multi-turn question about this job's purpose, status, progress, "
            "published results, or missing evidence. This never starts a Run."
        ),
        structured_output=True,
    )
    async def ask_job(
        question: str,
        conversation_id: str | None = None,
        request_id: str | None = None,
        stream: bool = False,
        background: bool = False,
        reply_action: str = "start",
        assistance_task: AssistanceTask | None = None,
        ctx: Context = None,
    ) -> dict[str, Any] | InputRequiredResult:
        if ctx is not None:
            pending = resolve_human_input(ctx)
            if pending is not None:
                return pending
        job_id = bound_job_id()
        if background:
            return await anyio.to_thread.run_sync(
                lambda: _reply_rpc(
                    lambda: context_provider.response_work_command(
                        job_id,
                        question,
                        conversation_id=conversation_id,
                        request_id=request_id,
                        action=reply_action,
                        assistance_task=assistance_task,
                    )
                )
            )
        if stream:
            if ctx is None:
                raise ValueError("Streaming requires an MCP request context")
            from mn_api.job_reply_stream import stream_job_reply

            return await stream_job_reply(
                context_provider, job_id, question, conversation_id, request_id, ctx, assistance_task=assistance_task
            )
        return await anyio.to_thread.run_sync(
            lambda: context_provider.ask_job(
                job_id,
                question,
                conversation_id=conversation_id,
                request_id=request_id,
                assistance_task=assistance_task,
            )
        )

    @agent_server.tool(
        name="ask_job",
        description=(
            "Ask the bounded Job agent a grounded question or an explicit simulation command. "
            "This never starts or resumes a Run."
        ),
        structured_output=True,
    )
    def ask_agent_job(
        question: str,
        conversation_id: str | None = None,
        request_id: str | None = None,
        background: bool = False,
        reply_action: str = "start",
        assistance_task: AssistanceTask | None = None,
        ctx: Context = None,
    ) -> dict[str, Any] | InputRequiredResult:
        if ctx is not None:
            pending = resolve_human_input(ctx)
            if pending is not None:
                return pending
        if background:
            return _reply_rpc(
                lambda: context_provider.response_work_command(
                    bound_job_id(),
                    question,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    action=reply_action,
                    assistance_task=assistance_task,
                )
            )
        return context_provider.ask_job(
            bound_job_id(),
            question,
            conversation_id=conversation_id,
            request_id=request_id,
            assistance_task=assistance_task,
        )

    @agent_server.tool(
        name="get_job_turn",
        description="Poll one accepted bounded-agent turn until its observed operation reaches a terminal state.",
        structured_output=True,
    )
    def get_job_turn(turn_id: str) -> dict[str, Any]:
        return _reply_rpc(lambda: context_provider.get_job_turn(bound_job_id(), turn_id))

    supervision.resolve_human_input = resolve_human_input
    servers = [base_server, enhanced_server, agent_server, stream_server]
    return servers, JobMCPGuard(
        base_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
        enhanced_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
        agent_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
        context_provider,
        stream_app=stream_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=False,
            stateless_http=True,
        ),
    )


def job_mcp_lifespan(servers: MCPServer | list[MCPServer]):
    resolved_servers = servers if isinstance(servers, list) else [servers]

    @asynccontextmanager
    async def lifespan(_app):
        if len(resolved_servers) == 1:
            async with resolved_servers[0].session_manager.run():
                yield
            return
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            for server in resolved_servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    return lifespan
