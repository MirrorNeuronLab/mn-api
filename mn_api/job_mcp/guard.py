"""Bind job MCP requests to an authenticated session."""

from __future__ import annotations

import hmac
import json

import anyio
from starlette.requests import Request

from mn_api import state
from mn_api.errors import problem_response
from mn_api.runtime_mcp import JobContextProvider

from .context import JobMCPNotFoundError, JobMCPUnavailableError, _current_job_id, _first_text


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
