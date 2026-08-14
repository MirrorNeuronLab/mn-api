from __future__ import annotations

import contextvars
import hmac
import json
import re
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Mapping

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from mn_api import state
from mn_api.blueprints import find_blueprint
from mn_api.config import auth_enabled
from mn_api.errors import problem_response
from mn_api.public import decode, public_value, records
from mn_api.routes import jobs as runtime_job_routes
from mn_api.routes import runs as runtime_run_routes
from mn_sdk import RuntimeService


STABLE_JOB_CONTEXT_SCHEMA = "mn.mcp.stable_job_context.v1"
MAX_CONTEXT_BYTES = 256 * 1024
MAX_EVIDENCE_RECORDS = 50
_ACTIVE_RUN_STATUSES = {"pending", "validated", "running", "pausing", "resuming"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "canceled", "deleted"}
_ACTIVE_SCHEDULE_STATUSES = {"active", "enabled", "running", "scheduled"}
_SENSITIVE_KEY_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "session",
    "token",
}
_PATH_KEY_PARTS = {"bundle_dir", "bundle_path", "host_path", "local_path", "run_dir", "runs_root"}
_OMITTED_PAYLOAD_KEYS = {
    "artifact_bodies",
    "artifact_body",
    "blob",
    "body",
    "bytes",
    "content",
    "contents",
    "env",
    "environment",
    "file_contents",
    "files",
    "logs",
    "raw",
    "raw_logs",
    "stderr",
    "stdout",
}
_current_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("stable_job_mcp_job_id", default="")


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
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _sensitive_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return any(part == normalized or part in normalized for part in _SENSITIVE_KEY_PARTS)


def _path_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized == "path" or normalized.endswith("_path") or normalized in _PATH_KEY_PARTS


def _omitted_payload_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _OMITTED_PAYLOAD_KEYS
        or normalized.endswith("_environment")
        or normalized.endswith("_logs")
    )


def _safe_text(value: Any, *, limit: int = 4_000) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("file://") or text.startswith("/") or re.match(r"^[a-zA-Z]:[\\/]", text):
        return "<redacted-path>"
    if "://" in text:
        try:
            parsed = urllib.parse.urlsplit(text)
            if parsed.username is not None or parsed.password is not None:
                return "<redacted-credential-url>"
        except ValueError:
            return "<redacted-url>"
    return f"{text[: limit - 3]}..." if len(text) > limit else text


def safe_context_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, secret- and host-path-free MCP projection."""
    if depth >= 12:
        return "<truncated>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)
            if (
                key == "version"
                or key.startswith("_")
                or _sensitive_key(key)
                or _path_key(key)
                or _omitted_payload_key(key)
            ):
                continue
            result[key] = safe_context_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [safe_context_value(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return _safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4_000]


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


def _identity(job_id: str, blueprint_id: str, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    identity = {"job_id": _safe_text(job_id, limit=512), "blueprint_id": _safe_text(blueprint_id, limit=512)}
    if descriptor.get("goal_id"):
        identity["goal_id"] = _safe_text(descriptor["goal_id"], limit=512)
    return identity


def _schedule_for_job(service: RuntimeService, job_id: str) -> list[dict[str, Any]]:
    payload = service.list_schedules(job_id=job_id)
    return [
        safe_context_value(schedule)
        for schedule in records(payload, "items", "schedules", "data")
        if _first_text(schedule.get("job_id"), schedule.get("jobId"), schedule.get("target_job_id")) == job_id
    ][:20]


def _latest_run_record(service: RuntimeService, job: Mapping[str, Any], job_id: str) -> dict[str, Any] | None:
    latest_run_id = _first_text(job.get("latest_run_id"), job.get("latestRunId"))
    if latest_run_id:
        return _as_record(service.get_run(latest_run_id))
    run_items = records(service.list_runs(job_id, page_size=1, page_token=""), "items", "runs", "data")
    return run_items[0] if run_items else None


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "run_id",
        "job_id",
        "status",
        "created_at",
        "submitted_at",
        "started_at",
        "updated_at",
        "completed_at",
        "finished_at",
        "run_type",
        "trigger",
        "failure",
    )
    return safe_context_value({key: run.get(key) for key in allowed if run.get(key) is not None})


def _evidence_from_workflow(workflow: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for step in list(workflow.get("steps") or [])[:limit]:
        if not isinstance(step, Mapping):
            continue
        record_id = _first_text(step.get("id"), step.get("step_id"), step.get("name"))
        title = _first_text(step.get("summary"), step.get("message"), step.get("title"), step.get("name"), record_id)
        if not title:
            continue
        evidence.append(
            {
                "kind": "status",
                "record_id": record_id or f"step-{len(evidence) + 1}",
                "summary": title[:800],
                "status": _first_text(step.get("status"), step.get("state")) or "unknown",
                "publication_state": "final" if _first_text(step.get("status")).lower() in _TERMINAL_RUN_STATUSES else "staged",
                "published_at": _first_text(step.get("updated_at"), step.get("completed_at"), step.get("started_at")) or None,
            }
        )
    return evidence


def _active_schedule(schedules: list[dict[str, Any]]) -> bool:
    return any(_first_text(item.get("status"), item.get("state")).lower() in _ACTIVE_SCHEDULE_STATUSES for item in schedules)


def context_state(job: Mapping[str, Any], latest_run: Mapping[str, Any] | None, schedules: list[dict[str, Any]]) -> str:
    if _first_text(job.get("status")).lower() == "archived":
        return "archived"
    run_status = _first_text((latest_run or {}).get("status")).lower()
    if run_status in _ACTIVE_RUN_STATUSES:
        return "running"
    if run_status == "paused":
        return "paused"
    if _active_schedule(schedules) or run_status == "scheduled":
        return "scheduled_waiting"
    if not latest_run:
        return "never_run"
    return "idle"


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8"))


def _fit_context(context: dict[str, Any]) -> dict[str, Any]:
    context.setdefault("truncation", {"truncated": False, "max_bytes": MAX_CONTEXT_BYTES})
    if _encoded_size(context) <= MAX_CONTEXT_BYTES:
        return context

    context["truncation"]["truncated"] = True
    context.setdefault("warnings", []).append("The stable job context was truncated to the MCP response limit.")
    evidence = context.get("evidence") if isinstance(context.get("evidence"), list) else []
    while evidence and _encoded_size(context) > MAX_CONTEXT_BYTES:
        del evidence[: max(1, len(evidence) // 2)]
    profile = context.get("profile") if isinstance(context.get("profile"), dict) else {}
    if _encoded_size(context) > MAX_CONTEXT_BYTES and "configuration" in profile:
        profile["configuration"] = {"truncated": True}
    latest = context.get("latest_run") if isinstance(context.get("latest_run"), dict) else {}
    if _encoded_size(context) > MAX_CONTEXT_BYTES and "result" in latest:
        latest["result"] = {"truncated": True}
    if _encoded_size(context) > MAX_CONTEXT_BYTES and "workflow" in latest:
        latest["workflow"] = {"truncated": True}
    schedules = context.get("schedules") if isinstance(context.get("schedules"), list) else []
    while schedules and _encoded_size(context) > MAX_CONTEXT_BYTES:
        schedules.pop()
    if _encoded_size(context) > MAX_CONTEXT_BYTES:
        profile["capabilities"] = list(profile.get("capabilities") or [])[:5]
        profile["mission"] = _safe_text(profile.get("mission"), limit=1_000)
        profile["expected_output"] = _safe_text(profile.get("expected_output"), limit=1_000)
    if _encoded_size(context) > MAX_CONTEXT_BYTES:
        context["latest_run"] = {
            key: _safe_text(value, limit=512) if isinstance(value, str) else value
            for key, value in latest.items()
            if key in {"run_id", "job_id", "status", "created_at", "started_at", "updated_at", "completed_at", "finished_at"}
        } or None
        context["evidence"] = []
        context["schedules"] = []
    context["truncation"]["evidence_returned"] = len(context.get("evidence") or [])
    if _encoded_size(context) > MAX_CONTEXT_BYTES:
        context = {
            "schema_version": STABLE_JOB_CONTEXT_SCHEMA,
            "fetched_at": context.get("fetched_at"),
            "identity": context.get("identity"),
            "state": context.get("state"),
            "read_only": True,
            "profile": {
                "identity": profile.get("identity"),
                "name": _safe_text(profile.get("name"), limit=512),
                "mission": _safe_text(profile.get("mission"), limit=1_000),
                "capabilities": [],
                "expected_output": _safe_text(profile.get("expected_output"), limit=1_000),
                "configuration": {"truncated": True},
                "archived": bool(profile.get("archived")),
            },
            "schedules": [],
            "latest_run": None,
            "evidence": [],
            "warnings": ["The stable job context was truncated to the MCP response limit."],
            "truncation": {
                **context.get("truncation", {}),
                "truncated": True,
                "max_bytes": MAX_CONTEXT_BYTES,
            },
        }
    return context


class StableJobContextProvider:
    def _service(self) -> RuntimeService:
        return RuntimeService(state.client)

    def _job_and_blueprint(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        try:
            job = public_value(_as_record(self._service().get_stable_job(job_id)))
        except Exception as error:
            if _is_not_found_error(error):
                raise JobMCPNotFoundError("The requested stable job MCP is unavailable.") from error
            raise JobMCPUnavailableError("Stable job context is temporarily unavailable.") from error
        if not job or _first_text(job.get("deleted_at")) or job.get("deleted") is True:
            raise JobMCPNotFoundError("The requested stable job MCP is unavailable.")
        blueprint_id = _first_text(job.get("blueprint_id"), job.get("blueprintId"))
        if not blueprint_id:
            raise JobMCPNotFoundError("The requested stable job MCP is unavailable.")
        try:
            _repo_root, blueprint = find_blueprint(state.refresh_config_from_env(), blueprint_id)
        except Exception as error:
            if _is_not_found_error(error):
                raise JobMCPNotFoundError("The requested stable job MCP is unavailable.") from error
            raise JobMCPUnavailableError("The stable job blueprint is temporarily unavailable.") from error
        descriptor = _mcp_descriptor(blueprint)
        if not descriptor["enabled"]:
            raise JobMCPNotFoundError("The requested stable job MCP is unavailable.")
        return job, blueprint, descriptor

    def validate(self, job_id: str) -> None:
        self._job_and_blueprint(job_id)

    def get_context(self, job_id: str, *, evidence_limit: int = MAX_EVIDENCE_RECORDS) -> dict[str, Any]:
        bounded_limit = max(1, min(MAX_EVIDENCE_RECORDS, int(evidence_limit or MAX_EVIDENCE_RECORDS)))
        job, blueprint, descriptor = self._job_and_blueprint(job_id)
        service = self._service()
        blueprint_id = _first_text(job.get("blueprint_id"), job.get("blueprintId"))
        warnings: list[str] = []
        try:
            schedules = _schedule_for_job(service, job_id)
        except Exception:
            schedules = []
            warnings.append("Schedule context is temporarily unavailable.")
        try:
            run = _latest_run_record(service, job, job_id)
        except Exception:
            run = None
            warnings.append("The latest run record is temporarily unavailable.")

        latest: dict[str, Any] | None = None
        evidence: list[dict[str, Any]] = []
        if run:
            latest = _run_summary(run)
            run_id = _first_text(run.get("run_id"), run.get("id"), job.get("latest_run_id"))
            runtime_run_id = _first_text(run.get("runtime_run_id"), run.get("runtimeRunId"), run_id)
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
                final_artifact = runtime_run_routes.get_run_final_artifact(runtime_run_id, "authenticated")
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
        context = {
            "schema_version": STABLE_JOB_CONTEXT_SCHEMA,
            "fetched_at": _now_iso(),
            "identity": identity,
            "state": state_name,
            "read_only": True,
            "profile": profile,
            "schedules": schedules,
            "latest_run": latest,
            "evidence": evidence[-bounded_limit:],
            "warnings": warnings,
            "truncation": {
                "truncated": len(evidence) > bounded_limit,
                "max_bytes": MAX_CONTEXT_BYTES,
                "evidence_limit": bounded_limit,
                "evidence_available": len(evidence),
                "evidence_returned": min(len(evidence), bounded_limit),
            },
        }
        return _fit_context(context)

    def get_profile(self, job_id: str) -> dict[str, Any]:
        context = self.get_context(job_id, evidence_limit=1)
        return {
            key: context[key]
            for key in ("schema_version", "fetched_at", "identity", "state", "read_only", "profile", "schedules", "warnings", "truncation")
        }

    def get_latest_run(self, job_id: str) -> dict[str, Any]:
        context = self.get_context(job_id)
        return {
            key: context[key]
            for key in ("schema_version", "fetched_at", "identity", "state", "read_only", "latest_run", "evidence", "warnings", "truncation")
        }


class StableJobMCPGuard:
    def __init__(self, app, provider: StableJobContextProvider) -> None:
        self.app = app
        self.provider = provider

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
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
            await anyio.to_thread.run_sync(self.provider.validate, job_id)
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
            await self.app(scope, receive, send)
        finally:
            _current_job_id.reset(token)


def create_stable_job_mcp(provider: StableJobContextProvider | None = None) -> tuple[FastMCP, Any]:
    context_provider = provider or StableJobContextProvider()
    server = FastMCP(
        "MirrorNeuron stable job context",
        instructions="Read-only supervisory context for the stable job bound by the MCP URL.",
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def bound_job_id() -> str:
        job_id = _current_job_id.get()
        if not job_id:
            raise JobMCPNotFoundError("The MCP request is not bound to a stable job.")
        return job_id

    @server.tool(
        name="get_job_profile",
        description="Read this stable job's identity, mission, safe configuration, schedule, and lifecycle state.",
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
        description="Read the combined stable job profile, schedule, and bounded latest-run context.",
        structured_output=True,
    )
    def get_job_context(evidence_limit: int = MAX_EVIDENCE_RECORDS) -> dict[str, Any]:
        return context_provider.get_context(bound_job_id(), evidence_limit=evidence_limit)

    return server, StableJobMCPGuard(server.streamable_http_app(), context_provider)


def stable_job_mcp_lifespan(server: FastMCP):
    @asynccontextmanager
    async def lifespan(_app):
        async with server.session_manager.run():
            yield

    return lifespan


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_EVIDENCE_RECORDS",
    "STABLE_JOB_CONTEXT_SCHEMA",
    "StableJobContextProvider",
    "context_state",
    "create_stable_job_mcp",
    "safe_context_value",
    "stable_job_mcp_lifespan",
]
