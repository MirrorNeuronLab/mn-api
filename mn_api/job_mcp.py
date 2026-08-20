from __future__ import annotations

import contextvars
import hmac
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Mapping

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from mn_api import state
from mn_api.blueprints import find_blueprint
from mn_api.config import auth_enabled
from mn_api.errors import problem_response
from mn_api.public import decode, public_value
from mn_api.routes import jobs as runtime_job_routes
from mn_api.routes import runs as runtime_run_routes
from mn_sdk import RuntimeConfig, RuntimeService
from mn_sdk.staged_artifacts import is_staged_artifact_ref, resolve_json_reference
from mn_sdk.job_context import (
    JOB_CONTEXT_SCHEMA,
    MAX_CONTEXT_BYTES,
    MAX_EVIDENCE_RECORDS,
    MAX_RECENT_RUNS,
    assemble_job_context,
    context_state as sdk_context_state,
    evidence_from_workflow as sdk_evidence_from_workflow,
    fit_context as sdk_fit_context,
    now_iso as sdk_now_iso,
    run_summary as sdk_run_summary,
    safe_context_value as sdk_safe_context_value,
    safe_text as sdk_safe_text,
)


_ACTIVE_SCHEDULE_STATUSES = {"active", "enabled", "running", "scheduled"}
_current_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_mcp_job_id", default="")


class JobMCPNotFoundError(RuntimeError):
    pass


class JobMCPUnavailableError(RuntimeError):
    pass


def _as_record(value: Any) -> dict[str, Any]:
    decoded = decode(value)
    return decoded if isinstance(decoded, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _now_iso() -> str:
    return sdk_now_iso()


def _safe_text(value: Any, *, limit: int = 4_000) -> str:
    return sdk_safe_text(value, limit=limit)


def safe_context_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, secret- and host-path-free MCP projection."""
    return sdk_safe_context_value(value, depth=depth)


def _descriptor_sources(blueprint: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    metadata = blueprint.get("metadata") if isinstance(blueprint.get("metadata"), Mapping) else {}
    product_value = blueprint.get("product") or metadata.get("product")
    product = product_value if isinstance(product_value, Mapping) else {}
    sources: list[Mapping[str, Any]] = []
    for owner in (blueprint, metadata, product):
        for key in ("mcpCollaboration", "mcp_collaboration"):
            value = owner.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
    return sources


def _mcp_descriptor(blueprint: Mapping[str, Any]) -> dict[str, Any]:
    descriptor = next(iter(_descriptor_sources(blueprint)), {})
    service_name = _first_text(descriptor.get("service_name"), descriptor.get("serviceName"), "mn-job-collaboration")
    transport = _first_text(descriptor.get("transport"), "streamable-http")
    path = _first_text(descriptor.get("path"), "/mcp")
    enabled = bool(
        descriptor.get("enabled") is True
        and service_name == "mn-job-collaboration"
        and transport == "streamable-http"
        and path == "/mcp"
    )
    return {
        "enabled": enabled,
        "goal_id": _first_text(descriptor.get("goal_id"), descriptor.get("goalId")) or None,
    }


def _response_service_declared(blueprint: Mapping[str, Any]) -> bool:
    value = blueprint.get("response_service")
    return isinstance(value, Mapping) and value.get("enabled") is True


def _response_service_enabled(job: Mapping[str, Any], blueprint: Mapping[str, Any]) -> bool:
    projected = job.get("response_service")
    if isinstance(projected, Mapping):
        state_name = _first_text(projected.get("state")).lower()
        if state_name == "disabled":
            return False
        if state_name in {"starting", "ready", "degraded", "failed"}:
            return True
        if projected.get("enabled") is True:
            return True
    return _response_service_declared(blueprint)


def _blueprint_field(blueprint: Mapping[str, Any], *keys: str) -> Any:
    metadata = blueprint.get("metadata") if isinstance(blueprint.get("metadata"), Mapping) else {}
    product_value = blueprint.get("product") or metadata.get("product")
    product = product_value if isinstance(product_value, Mapping) else {}
    for owner in (blueprint, metadata, product):
        for key in keys:
            value = owner.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _is_not_found_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None) or getattr(error, "status", None)
    code = _first_text(getattr(error, "code", None)).lower()
    message = str(error).lower()
    return status_code == 404 or "not_found" in code or "not found" in message


def _is_request_conflict_error(error: Exception) -> bool:
    code = _first_text(getattr(error, "code", None), getattr(error, "status", None)).lower()
    message = str(error).lower()
    return "already_exists" in code or "request_id_conflict" in message


def _identity(job_id: str, blueprint_id: str, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    identity = {"job_id": _safe_text(job_id, limit=512), "blueprint_id": _safe_text(blueprint_id, limit=512)}
    if descriptor.get("goal_id"):
        identity["goal_id"] = _safe_text(descriptor["goal_id"], limit=512)
    return identity


def _schedules_for_job(job: Mapping[str, Any], job_id: str) -> list[dict[str, Any]]:
    payload = job.get("schedules")
    schedules = payload if isinstance(payload, list) else []
    return [
        safe_context_value(schedule)
        for schedule in schedules
        if isinstance(schedule, Mapping)
        if _first_text(schedule.get("job_id"), schedule.get("jobId"), schedule.get("target_job_id")) == job_id
    ][:20]


def _recent_run_records(service: RuntimeService, job: Mapping[str, Any], job_id: str) -> list[dict[str, Any]]:
    recent_ids = job.get("recent_run_ids")
    if isinstance(recent_ids, list) and recent_ids:
        records_by_id: list[dict[str, Any]] = []
        for run_id in recent_ids[:MAX_RECENT_RUNS]:
            if _first_text(run_id):
                try:
                    record = _as_record(service.get_run(str(run_id)))
                except Exception:
                    continue
                if record:
                    records_by_id.append(record)
        return [item for item in records_by_id if item][:MAX_RECENT_RUNS]
    # Keep the internal staged-artifact reference intact until it has been
    # resolved. The generic public projection intentionally removes `version`
    # fields, which would make a valid `mn.staged_artifact/v1` reference
    # impossible to recognize here. Only bounded summaries and the resolved,
    # sanitized result leave this provider.
    listed = decode(service.list_runs(job_id, page_size=MAX_RECENT_RUNS, page_token=""))
    listed_record = listed if isinstance(listed, Mapping) else {}
    raw_items = next(
        (
            listed_record.get(key)
            for key in ("items", "runs", "data")
            if isinstance(listed_record.get(key), list)
        ),
        listed if isinstance(listed, list) else [],
    )
    run_items = [dict(item) for item in raw_items if isinstance(item, Mapping)][:MAX_RECENT_RUNS]
    latest_run_id = _first_text(job.get("latest_run_id"), job.get("latestRunId"))
    if latest_run_id and not any(_first_text(item.get("run_id"), item.get("id")) == latest_run_id for item in run_items):
        run_items.insert(0, _as_record(service.get_run(latest_run_id)))
    return [item for item in run_items if item][:MAX_RECENT_RUNS]


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    return sdk_run_summary(run)


def _runtime_output_id(run: Mapping[str, Any], public_run_id: str) -> str:
    for key in ("runtime_run_id", "runtimeRunId", "runtime_job_id", "output_run_id"):
        value = _first_text(run.get(key))
        if value:
            return value
    for key in ("result_ref", "workflow_state_ref"):
        reference = run.get(key)
        if not isinstance(reference, Mapping):
            continue
        value = _first_text(reference.get("run_id"), reference.get("runtime_run_id"))
        if value:
            return value
    return public_run_id


def _final_artifact_for_run(run: Mapping[str, Any], runtime_run_id: str) -> Any:
    try:
        return runtime_run_routes.get_run_final_artifact(runtime_run_id, "authenticated")
    except Exception as error:
        if not _is_not_found_error(error):
            raise
        reference = run.get("result_ref")
        if not is_staged_artifact_ref(reference):
            result = run.get("result")
            reference = result.get("result_ref") if isinstance(result, Mapping) else None
        if not is_staged_artifact_ref(reference):
            raise
        resolution_env = dict(os.environ)
        resolution_env.setdefault(
            "MN_HOST_SHARED_STORAGE_ROOT",
            RuntimeConfig.from_env().shared_storage_root,
        )
        return resolve_json_reference(reference, env=resolution_env)


def _evidence_from_workflow(workflow: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    return sdk_evidence_from_workflow(workflow, limit)


def _active_schedule(schedules: list[dict[str, Any]]) -> bool:
    return any(_first_text(item.get("status"), item.get("state")).lower() in _ACTIVE_SCHEDULE_STATUSES for item in schedules)


def context_state(job: Mapping[str, Any], latest_run: Mapping[str, Any] | None, schedules: list[dict[str, Any]]) -> str:
    return sdk_context_state(job, latest_run, schedules)


def _fit_context(context: dict[str, Any]) -> dict[str, Any]:
    return sdk_fit_context(context)


def _fallback_job_answer(
    question: str,
    context: Mapping[str, Any],
    *,
    conversation_id: str | None,
    request_id: str | None,
) -> dict[str, Any]:
    del question
    identity = context.get("identity") if isinstance(context.get("identity"), Mapping) else {}
    profile = context.get("profile") if isinstance(context.get("profile"), Mapping) else {}
    latest = context.get("latest_run") if isinstance(context.get("latest_run"), Mapping) else None
    state_name = _first_text(context.get("state")) or "unknown"
    name = _first_text(profile.get("name"), identity.get("blueprint_id"), "This job")
    lines = [f"{name} is currently {state_name.replace('_', ' ')}."]
    mission = _safe_text(profile.get("mission"), limit=1_200)
    if mission:
        lines.append(f"Its declared purpose is: {mission}")
    if latest:
        lines.append(
            f"The latest run ({_first_text(latest.get('run_id'), 'latest')}) is "
            f"{_first_text(latest.get('status'), 'unknown')}."
        )
    else:
        lines.append("It has not started a run yet, so there is no run progress or result to report.")
    lines.append(
        "This is a grounded status summary; the semantic answer service was unavailable, "
        "so no additional conclusion was inferred."
    )
    citations = []
    for item in list(context.get("evidence") or [])[:20]:
        if not isinstance(item, Mapping):
            continue
        citations.append(
            {
                "kind": _safe_text(item.get("kind"), limit=100) or "job_context",
                "record_id": _safe_text(item.get("record_id"), limit=200) or "evidence",
                "summary": _safe_text(item.get("summary"), limit=800),
                "status": _safe_text(item.get("status"), limit=100),
            }
        )
    response = {
        "schema_version": "mn.mcp.job_answer.v1",
        "answer": "\n\n".join(lines)[:12_000],
        "conversation_id": conversation_id or str(uuid.uuid4()),
        "request_id": request_id or None,
        "job_id": _first_text(identity.get("job_id")),
        "state": {
            "job": state_name,
            "latest_run": (
                {
                    key: latest.get(key)
                    for key in ("run_id", "status", "started_at", "updated_at", "completed_at", "finished_at")
                    if latest.get(key) is not None
                }
                if latest
                else None
            ),
        },
        "citations": citations,
        "warnings": ["A deterministic answer was returned because the response service was unavailable."],
        "service": {"state": "degraded"},
        "model": {"used": False, "fallback": True},
        "conversation_persisted": False,
    }
    while response["citations"] and len(_json_bytes(response)) > 64 * 1024:
        response["citations"].pop()
    if len(_json_bytes(response)) > 64 * 1024:
        response["state"]["latest_run"] = None
        response["answer"] = response["answer"][:4_000]
    return response


def _json_bytes(value: Any) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


class JobContextProvider:
    def _service(self) -> RuntimeService:
        return RuntimeService(state.client)

    def _job_and_blueprint(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        try:
            job = public_value(_as_record(self._service().get_job(job_id)))
        except Exception as error:
            if _is_not_found_error(error):
                raise JobMCPNotFoundError("The requested job MCP is unavailable.") from error
            raise JobMCPUnavailableError("Job context is temporarily unavailable.") from error
        if not job or _first_text(job.get("deleted_at")) or job.get("deleted") is True:
            raise JobMCPNotFoundError("The requested job MCP is unavailable.")
        blueprint_id = _first_text(job.get("blueprint_id"), job.get("blueprintId"))
        if not blueprint_id:
            raise JobMCPNotFoundError("The requested job MCP is unavailable.")
        try:
            _repo_root, blueprint = find_blueprint(state.refresh_config_from_env(), blueprint_id)
        except Exception as error:
            if _is_not_found_error(error):
                raise JobMCPNotFoundError("The requested job MCP is unavailable.") from error
            raise JobMCPUnavailableError("The job blueprint is temporarily unavailable.") from error
        descriptor = _mcp_descriptor(blueprint)
        descriptor["response_enabled"] = _response_service_enabled(job, blueprint)
        if not descriptor["enabled"] and not descriptor["response_enabled"]:
            raise JobMCPNotFoundError("The requested job MCP is unavailable.")
        return job, blueprint, descriptor

    def validate(self, job_id: str) -> None:
        self._job_and_blueprint(job_id)

    def response_enabled(self, job_id: str) -> bool:
        _job, _blueprint, descriptor = self._job_and_blueprint(job_id)
        return bool(descriptor.get("response_enabled"))

    def get_context(self, job_id: str, *, evidence_limit: int = MAX_EVIDENCE_RECORDS) -> dict[str, Any]:
        bounded_limit = max(1, min(MAX_EVIDENCE_RECORDS, int(evidence_limit or MAX_EVIDENCE_RECORDS)))
        job, blueprint, descriptor = self._job_and_blueprint(job_id)
        service = self._service()
        blueprint_id = _first_text(job.get("blueprint_id"), job.get("blueprintId"))
        warnings: list[str] = []
        schedules = _schedules_for_job(job, job_id)
        try:
            recent_run_records = _recent_run_records(service, job, job_id)
            run = recent_run_records[0] if recent_run_records else None
        except Exception:
            recent_run_records = []
            run = None
            warnings.append("The latest run record is temporarily unavailable.")

        latest: dict[str, Any] | None = None
        evidence: list[dict[str, Any]] = []
        if run:
            latest = _run_summary(run)
            run_id = _first_text(run.get("run_id"), run.get("id"), job.get("latest_run_id"))
            runtime_run_id = _runtime_output_id(run, run_id)
            if run_id:
                latest["run_id"] = run_id
            try:
                workflow = public_value(runtime_job_routes._workflow_progress_snapshot_for_job(runtime_run_id))
                latest["workflow"] = safe_context_value(
                    {
                        key: workflow.get(key)
                        for key in ("status", "current_step_id", "completed_steps", "total_steps", "steps", "failure")
                        if workflow.get(key) is not None
                    }
                )
                evidence.extend(_evidence_from_workflow(workflow, bounded_limit))
            except Exception:
                warnings.append("Workflow evidence for the latest run is temporarily unavailable.")
            try:
                final_artifact = _final_artifact_for_run(run, runtime_run_id)
                latest["result"] = safe_context_value(final_artifact)
                evidence.append(
                    {
                        "kind": "result",
                        "record_id": "final-result",
                        "summary": "The latest run produced a final result.",
                        "publication_state": "final",
                        "published_at": _first_text(latest.get("completed_at"), latest.get("finished_at"), latest.get("updated_at")) or None,
                    }
                )
            except Exception as error:
                if not _is_not_found_error(error):
                    warnings.append("The final result for the latest run is temporarily unavailable.")

        state_name = context_state(job, run, schedules)
        identity = _identity(job_id, blueprint_id, descriptor)
        profile = {
            "identity": identity,
            "name": _safe_text(_first_text(
                job.get("display_name"),
                _blueprint_field(blueprint, "name"),
                job.get("job_name"),
                blueprint_id,
            )),
            "mission": _safe_text(_first_text(
                _blueprint_field(blueprint, "mission", "business_goal", "businessGoal", "description", "summary", "tagline")
            )),
            "capabilities": safe_context_value(_blueprint_field(blueprint, "capabilities") or []),
            "expected_output": _safe_text(_first_text(_blueprint_field(blueprint, "output", "expected_output", "expectedOutput"))),
            "configuration": safe_context_value(job.get("resolved_configuration") or job.get("resolvedConfiguration") or {}),
            "archived": state_name == "archived",
        }
        return assemble_job_context(
            identity=identity,
            state=state_name,
            profile=profile,
            schedules=schedules,
            latest_run=latest,
            recent_runs=[_run_summary(item) for item in recent_run_records],
            evidence=evidence,
            warnings=warnings,
            response_service=(
                job.get("response_service")
                if isinstance(job.get("response_service"), Mapping)
                else {"state": "disabled"}
            ),
            evidence_limit=bounded_limit,
        )

    def get_profile(self, job_id: str) -> dict[str, Any]:
        context = self.get_context(job_id, evidence_limit=1)
        return {
            key: context[key]
            for key in ("schema_version", "fetched_at", "identity", "state", "read_only", "response_service", "profile", "schedules", "warnings", "truncation")
        }

    def get_latest_run(self, job_id: str) -> dict[str, Any]:
        context = self.get_context(job_id)
        return {
            key: context[key]
            for key in ("schema_version", "fetched_at", "identity", "state", "read_only", "response_service", "latest_run", "recent_runs", "evidence", "warnings", "truncation")
        }

    def ask_job(
        self,
        job_id: str,
        question: str,
        *,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        question = str(question or "").strip()
        if not question:
            raise ValueError("question is required")
        if len(question) > 8_000:
            raise ValueError("question must not exceed 8000 characters")
        if conversation_id:
            try:
                conversation_id = str(uuid.UUID(conversation_id))
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError("conversation_id must be a UUID") from error
        if request_id is not None and len(str(request_id)) > 128:
            raise ValueError("request_id must not exceed 128 characters")
        if not self.response_enabled(job_id):
            raise JobMCPNotFoundError("The requested job response service is unavailable.")
        context = self.get_context(job_id)
        try:
            return self._service().query_job_response(
                job_id,
                question,
                context=context,
                conversation_id=conversation_id or "",
                request_id=request_id or "",
            )
        except (ValueError, TypeError):
            raise
        except Exception as error:
            if _is_request_conflict_error(error):
                raise ValueError("request_id was already used for a different question") from error
            return _fallback_job_answer(
                question,
                context,
                conversation_id=conversation_id,
                request_id=request_id,
            )


class JobMCPGuard:
    def __init__(self, base_app, enhanced_app, provider: JobContextProvider) -> None:
        self.base_app = base_app
        self.enhanced_app = enhanced_app
        self.provider = provider

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.base_app(scope, receive, send)
            return
        request = Request(scope)
        if auth_enabled(state.config):
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            expected = str(state.config.api_token or "")
            if scheme.lower() != "bearer" or not token or not hmac.compare_digest(token, expected):
                response = problem_response(
                    status_code=401,
                    error="unauthorized",
                    title="Authentication required",
                    detail="Missing or invalid bearer token.",
                    instance=request.url.path,
                    request_id=str(getattr(request.state, "request_id", "")),
                )
                await response(scope, receive, send)
                return

        job_id = _first_text(scope.get("path_params", {}).get("job_id"))
        try:
            enhanced = await anyio.to_thread.run_sync(self.provider.response_enabled, job_id)
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
        try:
            selected_app = self.enhanced_app if enhanced else self.base_app
            await selected_app(scope, receive, send)
        finally:
            _current_job_id.reset(token)


def create_job_mcp(provider: JobContextProvider | None = None) -> tuple[list[FastMCP], Any]:
    context_provider = provider or JobContextProvider()

    def new_server(name: str, instructions: str) -> FastMCP:
        return FastMCP(
            name,
            instructions=instructions,
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    base_server = new_server(
        "MirrorNeuron job context",
        "Read-only supervisory context for the job bound by the MCP URL.",
    )
    enhanced_server = new_server(
        "MirrorNeuron real-time job response",
        "Read safe context or ask grounded, multi-turn questions about the job bound by the MCP URL.",
    )

    def bound_job_id() -> str:
        job_id = _current_job_id.get()
        if not job_id:
            raise JobMCPNotFoundError("The MCP request is not bound to a job.")
        return job_id

    def register_context_tools(server: FastMCP) -> None:
        @server.tool(
            name="get_job_profile",
            description="Read this job's identity, mission, safe configuration, schedule, and lifecycle state.",
            structured_output=True,
        )
        def get_job_profile() -> dict[str, Any]:
            return context_provider.get_profile(bound_job_id())

        @server.tool(
            name="get_latest_run",
            description="Read bounded status, workflow evidence, and final result context from this job's latest run.",
            structured_output=True,
        )
        def get_latest_run() -> dict[str, Any]:
            return context_provider.get_latest_run(bound_job_id())

        @server.tool(
            name="get_job_context",
            description="Read the combined job profile, schedule, recent Runs, and bounded latest-run context.",
            structured_output=True,
        )
        def get_job_context(evidence_limit: int = MAX_EVIDENCE_RECORDS) -> dict[str, Any]:
            return context_provider.get_context(bound_job_id(), evidence_limit=evidence_limit)

    register_context_tools(base_server)
    register_context_tools(enhanced_server)

    @enhanced_server.tool(
        name="ask_job",
        description=(
            "Ask a fast, grounded, multi-turn question about this job's purpose, status, progress, "
            "published results, or missing evidence. This never starts a Run."
        ),
        structured_output=True,
    )
    def ask_job(
        question: str,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return context_provider.ask_job(
            bound_job_id(),
            question,
            conversation_id=conversation_id,
            request_id=request_id,
        )

    servers = [base_server, enhanced_server]
    return servers, JobMCPGuard(
        base_server.streamable_http_app(),
        enhanced_server.streamable_http_app(),
        context_provider,
    )


def job_mcp_lifespan(servers: FastMCP | list[FastMCP]):
    resolved_servers = servers if isinstance(servers, list) else [servers]

    @asynccontextmanager
    async def lifespan(_app):
        if len(resolved_servers) == 1:
            async with resolved_servers[0].session_manager.run():
                yield
            return
        async with resolved_servers[0].session_manager.run():
            async with resolved_servers[1].session_manager.run():
                yield

    return lifespan


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_EVIDENCE_RECORDS",
    "JOB_CONTEXT_SCHEMA",
    "JobContextProvider",
    "context_state",
    "create_job_mcp",
    "safe_context_value",
    "job_mcp_lifespan",
]
