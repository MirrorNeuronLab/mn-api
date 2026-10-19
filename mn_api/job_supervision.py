"""HTTP/runtime adapters for the SDK-owned MCP supervision registry."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar

import anyio
from mn_sdk import RuntimeService
from mn_sdk.integrations.job_control import transition_run
from mn_sdk_mcp.runtime_adapter import RuntimeMCPAdapter
from mn_sdk_mcp import JobSupervisor, OperationStore

from mn_api import state
from mn_api.path_utils import resolve_mn_home

native_launch_authority = ContextVar('mcp_native_launch_authority', default=False)


class BoundJobSupervision:
    def __init__(self, context_provider, bound_job_id):
        self.context_provider = context_provider
        self.bound_job_id = bound_job_id

    def _job(self):
        job_id = self.bound_job_id()
        job = RuntimeService(state.client).get_job(job_id)
        if job.get("job_id") != job_id or job.get("deleted") or job.get("deleted_at"):
            raise KeyError("Job not found")
        return job

    def identity(self):
        job = self._job()
        return {"job_id": job["job_id"], "blueprint_id": job["blueprint_id"]}

    def declaration(self):
        declaration = self._job().get("mcp")
        if not declaration:
            raise ValueError("The prepared Job requires an MCP contract update")
        return declaration

    def _supervisor(self):
        job = self._job()
        job_id, blueprint_id = job["job_id"], job["blueprint_id"]
        runtime = RuntimeService(state.client)
        adapter = self

        class RuntimeControls:
            get_job = runtime.get_job
            get_run = runtime.get_run

            def pause_run(self, run_id):
                return transition_run(runtime, job_id, run_id, "paused")

            def resume_run(self, run_id):
                return transition_run(runtime, job_id, run_id, "running")

            def cancel_run(self, run_id):
                return transition_run(runtime, job_id, run_id, "cancelled")

        def launch(request_id, metadata):
            from mn_api.api_models import RunCreate, BlueprintRunCreate
            from mn_api.operations import start_local_operation
            from mn_api.routes.v1.jobs import create_job_run_operation
            from mn_api.routes.v1.blueprints import create_blueprint_run

            if not isinstance(metadata, dict) or set(metadata) - {"config_overrides", "secret_environment", "force"}:
                raise ValueError("Invalid native launch metadata")
            configuration = metadata.get("config_overrides", {})
            secrets = metadata.get("secret_environment", {})
            if not secrets:
                response = create_job_run_operation(job_id, RunCreate(config_overrides=configuration), request_id, "mcp")
                return json.loads(response.body)
            request = BlueprintRunCreate(job_id=job_id, config_overrides=configuration,
                                         secret_environment=secrets, force=metadata.get("force", False))
            # Credentials are captured only by this in-memory callback. The
            # durable operation stores identity and progress, never this request.
            operation_id = "op-local-" + hashlib.sha256(f"mcp:{job_id}:{request_id}".encode()).hexdigest()

            def run(report_progress):
                report_progress(percent=10, stage="submit", label="Start run", detail="Submitting the run.")
                response = create_blueprint_run(blueprint_id, request, request_id, "mcp", None)
                return json.loads(response.body)

            return start_local_operation("create_job_run", {"job_id": job_id}, run, operation_id=operation_id, durable=True)

        async def activity(arguments, ctx):
            import time
            from mn_sdk_mcp import job_activity_input_required, resolve_job_activity_receipt
            if ctx is not None:
                receipt = resolve_job_activity_receipt(ctx)
                if receipt is not None:
                    return receipt
            cursor = arguments.get("after_event_id", "")
            deadline = time.monotonic() + arguments.get("wait_seconds", 20)
            while True:
                item = await anyio.to_thread.run_sync(lambda: adapter.context_provider.get_next_activity(
                    job_id, after_event_id=cursor, wait_seconds=max(0, deadline - time.monotonic())))
                if item is not None:
                    return job_activity_input_required(item, after_event_id=cursor)
                if time.monotonic() >= deadline:
                    return {"delivered": False, "cursor": cursor, "activity": None}
                await anyio.sleep(0.5)

        from mn_api.operations import get_operation
        def launch_binding(request_id, metadata):
            if metadata and not native_launch_authority.get():
                raise ValueError('Native launch metadata requires an internal desktop session.')
            if metadata.get("secret_environment"):
                value = f"mcp:{job_id}:{request_id}".encode()
            else:
                value = json.dumps(["mcp", f"/api/v1/jobs/{job_id}/run-operations", request_id], separators=(",", ":")).encode()
            return "op-local-" + hashlib.sha256(value).hexdigest()

        def poll_launch(operation_id):
            snapshot = get_operation(operation_id)
            result = snapshot.get("result") or {}
            receipt = {key: result[key] for key in ("job_id", "run_id", "execution_id", "runtime_run_id", "workflow_id", "graph_id", "status", "admission", "message") if key in result}
            projected = {"status": snapshot.get("status"), "progress": snapshot.get("progress", {})}
            if receipt:
                projected["result"] = receipt
            error = snapshot.get("error")
            if error:
                projected["failure"] = {key: error[key] for key in ("code", "retryable", "details", "problem_code", "category") if key in error}
            return projected
        store_path = resolve_mn_home() / "mcp_operations" / (hashlib.sha256(job_id.encode()).hexdigest() + ".sqlite3")
        return JobSupervisor(job_id=job_id, blueprint_id=blueprint_id, declaration=job["mcp"],
                             runtime=RuntimeControls(), context=self.context_provider, operations=OperationStore(store_path),
                             launch=launch, launch_binding=launch_binding, poll_launch=poll_launch, domain=RuntimeMCPAdapter(state.client), activity=activity)

    async def capabilities(self):
        return await anyio.to_thread.run_sync(lambda: self._supervisor().capabilities())

    async def invoke(self, name, arguments, ctx):
        if name == "get_job_context" and ctx is not None and getattr(self, "resolve_human_input", None):
            pending = self.resolve_human_input(ctx)
            if pending is not None:
                return pending
        result = await anyio.to_thread.run_sync(lambda: self._supervisor().invoke(name, arguments, ctx))
        import inspect
        return await result if inspect.isawaitable(result) else result
