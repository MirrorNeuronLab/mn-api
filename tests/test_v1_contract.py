from __future__ import annotations

import hashlib
import json
from threading import Event
import time
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.blueprint_additions import BlueprintAddError
from mn_api.contracts import API_CONTRACT
from mn_api.http_semantics import strong_etag
from mn_api.pagination import PageTokenRegistry, page
from mn_api.routes import jobs as internal_jobs
from mn_api.routes.v1 import blueprints, infrastructure, jobs, system


class CanonicalRuntime:
    def __init__(self):
        self.calls: list[tuple] = []
        self.run_status = "completed"

    def create_job(self, _manifest, _payloads, **kwargs):
        self.calls.append(("create_job", kwargs))
        return json.dumps({"version": 2, "job_id": kwargs.get("job_id") or "job-1", "status": "active", "revision": 1})

    def list_jobs(self, *, include_archived=False, page_size=50, page_token=""):
        self.calls.append(("list_jobs", include_archived))
        offset = int(page_token or 0)
        values = [
            {"job_id": f"job-{index:03}", "created_at": f"2026-01-01T00:{index:02}:00Z"}
            for index in range(75)
        ]
        selected = values[offset : offset + page_size]
        next_offset = offset + len(selected)
        return json.dumps(
            {
                "items": selected,
                "next_page_token": str(next_offset) if next_offset < len(values) else None,
            }
        )

    def get_job(self, job_id):
        return json.dumps({"job_id": job_id, "status": "active", "revision": 1})

    def archive_job(self, job_id, **_kwargs):
        return json.dumps({"job_id": job_id, "status": "archived", "revision": 2})

    def delete_job(self, job_id, **_kwargs):
        self.calls.append(("delete_job", job_id))
        return json.dumps({"job_id": job_id, "deleted": True})

    def update_job(self, job_id, attrs, **_kwargs):
        self.calls.append(("update_job", job_id, attrs))
        return json.dumps({"job_id": job_id, "status": "active", "revision": 2, **attrs})

    def start_run(self, job_id, **kwargs):
        self.calls.append(("start_run", job_id, kwargs))
        return json.dumps({"job_id": job_id, "run_id": kwargs.get("run_id") or "run-1", "status": "pending"})

    def list_runs(self, job_id, *, page_size=50, page_token=""):
        return json.dumps({"items": [{"job_id": job_id, "run_id": "run-1", "status": "completed"}], "next_page_token": None})

    def get_run(self, run_id):
        return json.dumps({"job_id": "job-1", "run_id": run_id, "status": self.run_status, "runtime_run_id": "runtime-1"})

    def pause_run(self, run_id):
        self.run_status = "paused"
        return json.dumps({"run_id": run_id, "status": "paused"})

    def resume_run(self, run_id):
        self.run_status = "running"
        return json.dumps({"run_id": run_id, "status": "running"})

    def cancel_run(self, run_id):
        self.run_status = "cancelled"
        return json.dumps({"run_id": run_id, "status": "cancelled"})

    def delete_run(self, run_id, **_kwargs):
        return json.dumps({"run_id": run_id, "deleted": True})

    def start_operation(self, kind, options):
        return json.dumps({"operation_id": f"op-{kind}", "kind": kind, "status": "pending", "options": options})

    def get_operation(self, operation_id):
        return json.dumps({"operation_id": operation_id, "status": "completed"})

    def stream_operation_events(self, operation_id, **_kwargs):
        yield json.dumps({"operation_id": operation_id, "type": "operation.completed", "status": "completed"})

    def create_job_schedule(self, job_id, **_kwargs):
        return json.dumps({"job_id": job_id, "schedule_id": "schedule-1", "status": "running", "revision": 1})

    def list_schedules(self, **_kwargs):
        return json.dumps({"items": [{"schedule_id": "schedule-1", "status": "running", "revision": 1}]})

    def get_schedule(self, schedule_id):
        return json.dumps({"schedule_id": schedule_id, "status": "running", "revision": 1})

    def pause_schedule(self, schedule_id, **_kwargs):
        return json.dumps({"schedule_id": schedule_id, "status": "paused", "revision": 2})

    def resume_schedule(self, schedule_id, **_kwargs):
        return json.dumps({"schedule_id": schedule_id, "status": "running", "revision": 2})

    def update_schedule(self, schedule_id, **_kwargs):
        return json.dumps({"schedule_id": schedule_id, "status": "running", "revision": 2})

    def delete_schedule(self, schedule_id, **_kwargs):
        return json.dumps({"schedule_id": schedule_id, "deleted": True})

    def dispatch_schedule(self, schedule_id, **_kwargs):
        return json.dumps({"schedule_id": schedule_id, "run_id": "run-dispatched", "status": "pending"})

    def emit_trigger_event(self, event_type, **_kwargs):
        return json.dumps({"event_id": "event-1", "event_type": event_type})

    def list_trigger_events(self, **_kwargs):
        return json.dumps({"items": [{"event_id": "event-1", "occurred_at": "2026-01-01T00:00:00Z"}]})

    def set_resource(self, payload):
        return json.dumps(payload)

    def remove_node(self, node_id):
        self.calls.append(("remove_node", node_id))

    def cancel_node_drain(self, node_id, **_kwargs):
        return json.dumps({"node": node_id, "draining": False})

    def set_node_maintenance(self, node_id, enabled, **kwargs):
        return json.dumps({"node": node_id, "enabled": enabled, **kwargs})

    def deploy_job(self, _manifest, _payloads, **kwargs):
        return json.dumps({"deployment_id": kwargs.get("deployment_key") or "deployment-1", "status": "running", "revision": 1})

    def list_deployments(self):
        return json.dumps({"items": [{"deployment_id": "deployment-1", "status": "running", "revision": 1}]})

    def get_deployment(self, deployment_id):
        return json.dumps({"deployment_id": deployment_id, "status": "running", "revision": 1})

    def pause_deployment(self, deployment_id, **_kwargs):
        return json.dumps({"deployment_id": deployment_id, "status": "paused", "revision": 2})

    def resume_deployment(self, deployment_id, **_kwargs):
        return json.dumps({"deployment_id": deployment_id, "status": "running", "revision": 2})

    def fail_deployment(self, deployment_id, **_kwargs):
        return json.dumps({"deployment_id": deployment_id, "status": "failed", "revision": 2})

    def promote_deployment(self, deployment_id):
        return json.dumps({"deployment_id": deployment_id, "promoted": True})

    def rollback_deployment(self, deployment_id, **_kwargs):
        return json.dumps({"deployment_id": deployment_id, "status": "rolling_back"})

    def list_services(self, **_kwargs):
        return json.dumps({"items": [{"id": "service-1", "name": "llm", "status": "passing"}]})

    def resolve_service(self, name, **_kwargs):
        return json.dumps({"items": [{"id": "service-1", "name": name, "status": "passing"}]})


def _client(monkeypatch) -> tuple[TestClient, CanonicalRuntime]:
    runtime = CanonicalRuntime()
    monkeypatch.setattr(state, "client", runtime)
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]),
    )
    manifest = json.dumps(
        {
            "apiVersion": "mn.workflow/v1",
            "kind": "Workflow",
            "id": "g",
            "name": "g",
            "manifest_version": "1.0",
            "job_name": "g",
            "contract": {"inputs": {}, "outputs": {"primary": {}}},
            "workflow": {
                "schema": "mn.workflow.problem_graph/v1",
                "workflow_id": "g",
                "entrypoint": "start",
                "source": "start",
                "sink": "start",
                "steps": [{"id": "start"}],
                "edges": [],
            },
            "agents": {"nodes": [], "edges": []},
            "runtime": {},
        }
    )
    monkeypatch.setattr(jobs, "load_uploaded_bundle", lambda *_args: (manifest, {}))
    return TestClient(create_app()), runtime


def test_job_manifest_decoder_selects_both_v1_forms_and_rejects_retired_versions():
    source = {
        "apiVersion": "mn.workflow/v1",
        "kind": "WorkflowSource",
        "identity": {"id": "source-api", "name": "Source API"},
        "defaults": {"worker": {"with": {"image": "python:3.11"}}},
        "workflow": {
            "steps": [
                {"id": "prepare", "needs": [], "run": {"handler": "source_api.prepare"}},
                {
                    "id": "publish",
                    "needs": ["prepare"],
                    "run": {"handler": "source_api.publish"},
                },
            ]
        },
    }
    executable = {
        "apiVersion": "mn.workflow/v1",
        "kind": "Workflow",
        "id": "executable-api",
        "contract": {},
        "agents": {},
        "runtime": {},
    }

    materialized = internal_jobs._decode_manifest(json.dumps(source))
    assert materialized["apiVersion"] == "mn.workflow/v1"
    assert materialized["kind"] == "Workflow"
    assert materialized["workflow"]["edges"][0]["from"] == "prepare"
    assert internal_jobs._decode_manifest(json.dumps(executable)) == executable

    try:
        internal_jobs._decode_manifest(json.dumps({"apiVersion": "mn.workflow/v2"}))
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "mn.workflow/v1" in str(exc.detail)
    else:
        raise AssertionError("retired workflow versions must be rejected")


def test_openapi_is_only_canonical_v1_and_documents_auth():
    schema = create_app().openapi()
    paths = schema["paths"]
    assert paths
    assert all(path.startswith("/api/v1") for path in paths)
    assert not any("/api/v2" in path or "/runtime-runs" in path or ":" in path or path.endswith("/ws") for path in paths)
    operation_ids = [
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    for path in ("/api/v1/jobs", "/api/v1/runs", "/api/v1/operations"):
        success_schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert success_schema["$ref"].endswith("/PageResponse")


def test_health_capability_and_removed_routes(monkeypatch):
    client, _runtime = _client(monkeypatch)
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "api_contract": API_CONTRACT, "auth": "disabled"}

    for path in (
        "/api/v2/health",
        "/api/v1/runtime-runs",
        "/api/v1/realtime",
        "/api/v1/schedules",
        "/api/v1/trigger-events",
        "/api/v1/deployments",
        "/api/v1/run-cleanups",
        "/api/v1/run-cancellations",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "not_found"


def test_strict_bodies_and_problem_details(monkeypatch):
    client, _runtime = _client(monkeypatch)
    response = client.post("/api/v1/jobs", json={"bundle_id": "bundle-1", "version": 2})
    assert response.status_code == 422
    problem = response.json()
    assert set(("type", "title", "status", "detail", "instance", "code", "request_id", "errors")) <= set(problem)
    assert problem["code"] == "validation_failed"


def test_collection_pagination_and_filter_binding(monkeypatch):
    client, _runtime = _client(monkeypatch)
    first = client.get("/api/v1/jobs?page_size=25").json()
    assert len(first["items"]) == 25
    assert first["next_page_token"]
    second = client.get(f"/api/v1/jobs?page_size=25&page_token={first['next_page_token']}").json()
    final = client.get(f"/api/v1/jobs?page_size=25&page_token={second['next_page_token']}").json()
    assert first["items"][-1]["job_id"] == "job-024"
    assert second["items"][0]["job_id"] == "job-025"
    assert final["next_page_token"] is None

    mismatch = client.get(
        f"/api/v1/jobs?include_archived=true&page_size=25&page_token={first['next_page_token']}"
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["code"] == "bad_request"


def test_page_snapshot_is_stable_under_inserts(monkeypatch):
    registry = PageTokenRegistry()
    monkeypatch.setattr("mn_api.pagination.page_tokens", registry)
    initial = [{"id": value} for value in ("b", "c", "d")]
    first = page(
        initial,
        route="/r",
        principal="p",
        filters={},
        page_size=2,
        sort_key="id",
        key=lambda item: item["id"],
        identity=lambda item: item["id"],
    )
    continued = page(
        [{"id": value} for value in ("a", "b", "c", "d", "e")],
        route="/r",
        principal="p",
        filters={},
        page_size=2,
        page_token=first["next_page_token"],
        sort_key="id",
        key=lambda item: item["id"],
        identity=lambda item: item["id"],
    )
    assert [item["id"] for item in continued["items"]] == ["d"]


def test_etag_precondition_and_idempotent_run_creation(monkeypatch):
    client, runtime = _client(monkeypatch)
    current = {"job_id": "job-1", "status": "active", "revision": 1}
    get_response = client.get("/api/v1/jobs/job-1")
    assert get_response.headers["etag"] == strong_etag(current)

    missing = client.patch("/api/v1/jobs/job-1", json={"display_name": "Changed"})
    assert missing.status_code == 428
    stale = client.patch(
        "/api/v1/jobs/job-1",
        headers={"If-Match": '"stale"'},
        json={"display_name": "Changed"},
    )
    assert stale.status_code == 412
    updated = client.patch(
        "/api/v1/jobs/job-1",
        headers={"If-Match": get_response.headers["etag"]},
        json={"display_name": "Changed"},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"]

    headers = {"Idempotency-Key": "start-1"}
    first = client.post("/api/v1/jobs/job-1/runs", headers=headers, json={"inputs": {"x": 1}})
    replay = client.post("/api/v1/jobs/job-1/runs", headers=headers, json={"inputs": {"x": 1}})
    assert first.status_code == replay.status_code == 202
    assert first.headers["location"] == "/api/v1/runs/run-1"
    assert replay.headers["idempotency-replayed"] == "true"
    assert sum(1 for call in runtime.calls if call[0] == "start_run") == 1

    conflict = client.post("/api/v1/jobs/job-1/runs", headers=headers, json={"inputs": {"x": 2}})
    assert conflict.status_code == 409


def _patch_canonical_projections(monkeypatch):
    blueprint = {"id": "worker-1", "name": "Worker", "installed": True, "revision": "abc"}
    monkeypatch.setattr(blueprints, "load_blueprint_catalog", lambda _config: (None, [blueprint]))
    monkeypatch.setattr(blueprints, "find_blueprint", lambda _config, _id: (None, blueprint))
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "validate_blueprint_inputs",
        lambda *_args, **_kwargs: {"ok": True, "issues": []},
    )
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "resolve_async_blueprint_run_request",
        lambda _id, request: request,
    )
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "run_blueprint_record",
        lambda *_args, **_kwargs: {"job_id": "job-blueprint", "run_id": "run-blueprint", "status": "pending"},
    )

    system_payload = {"status": "ok", "nodes": [{"node_name": "node-1"}]}
    monkeypatch.setattr(system.legacy_system, "runtime_status", lambda **_kwargs: system_payload)
    monkeypatch.setattr(system.legacy_system, "runtime_health", lambda **_kwargs: system_payload)
    monkeypatch.setattr(system.legacy_system, "runtime_doctor", lambda **_kwargs: system_payload)
    monkeypatch.setattr(system.legacy_system, "get_resource", lambda **_kwargs: {"cpu": 4})
    monkeypatch.setattr(system.legacy_system, "get_system_summary", lambda **_kwargs: system_payload)
    monkeypatch.setattr(system.legacy_system, "get_metrics", lambda **_kwargs: {"jobs": 1})
    monkeypatch.setattr(system.legacy_system, "get_nodes", lambda **_kwargs: system_payload)
    monkeypatch.setattr(system.legacy_system, "add_cluster_node", lambda *_args, **_kwargs: {"node_name": "node-2"})
    monkeypatch.setattr(system.legacy_system, "normalize_node_name", lambda value: value)

    monitor = {"job": {"job_id": "runtime-1", "status": "completed"}, "events": []}
    monkeypatch.setattr(jobs.runtime_job_routes, "_compact_job_detail", lambda _id: monitor)
    monkeypatch.setattr(
        jobs.runtime_job_routes,
        "_workflow_progress_snapshot_for_job",
        lambda _id: {"run_id": "runtime-1", "status": "completed", "steps": []},
    )
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_logs", lambda *_args: {"data": [{"id": "log-1"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", lambda *_args: {"data": [{"id": "event-1", "type": "done"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_resources", lambda *_args: {"cpu": []})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_human_events", lambda *_args: {"data": [{"request_id": "request-1"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "post_run_human_response", lambda *_args: {"request_id": "request-1", "status": "answered"})
    monkeypatch.setattr(jobs.runtime_run_routes, "post_run_human_ack", lambda *_args: {"request_id": "request-1", "status": "acknowledged"})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_ui", lambda *_args: {"ui": {"components": []}})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_ui_video", lambda *_args: {"video": True})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_final_artifact", lambda *_args: {"artifact_id": "final"})
    monkeypatch.setattr(jobs.runtime_run_routes, "list_run_artifacts", lambda *_args: {"artifacts": [{"artifact_id": "a"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_artifact", lambda *_args: {"download": "artifact"})
    monkeypatch.setattr(jobs.runtime_run_routes, "list_run_outputs", lambda *_args: {"outputs": [{"index": 0}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_output", lambda *_args: {"download": "output"})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_observability_summary", lambda *_args: {"trace_id": "trace-1"})
    monkeypatch.setattr(jobs.runtime_run_routes, "export_run", lambda *_args: {"run_id": "runtime-1"})
    monkeypatch.setattr(jobs, "build_agent_graph", lambda *_args: {"nodes": [], "edges": []})

    monkeypatch.setattr(infrastructure.model_routes, "list_runtime_models", lambda **_kwargs: {"models": [{"id": "model-1"}]})
    monkeypatch.setattr(infrastructure.model_routes, "show_runtime_model", lambda model_id: {"id": model_id})
    monkeypatch.setattr(infrastructure.model_routes, "benchmark_model", lambda model_id, request, _principal: {"id": model_id, **request})
    monkeypatch.setattr(infrastructure, "registered_model_records", lambda: [{"id": "remote-1", "source": "rest_remote"}])
    monkeypatch.setattr(infrastructure, "provider_registration", lambda model_id, **kwargs: {"id": model_id, **kwargs})
    monkeypatch.setattr(infrastructure, "remove_registered_model", lambda _name: (None, None))
    monkeypatch.setattr(infrastructure.model_routes, "_upsert_registry_record", lambda record: record)
    monkeypatch.setattr(infrastructure.model_routes, "_remote_projection", lambda _record: {"name": "remote-1", "model": "m"})
    monkeypatch.setattr(infrastructure.model_routes, "_proxy_projection", lambda _record: {"model_id": "proxy-1"})
    monkeypatch.setattr(infrastructure, "uploaded_bundle_root", lambda *_args: SimpleNamespace(__truediv__=lambda *_: None))
    monkeypatch.setattr(infrastructure, "run_service_validation", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        blueprints,
        "add_catalog_blueprint",
        lambda _config, blueprint_id, **_kwargs: {"added": True, "blueprint": {"id": blueprint_id}},
    )
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "uninstall_blueprints",
        lambda request, **_kwargs: {"removed": True, "blueprint_id": request.blueprint_id},
    )
    monkeypatch.setattr(
        blueprints,
        "refresh_blueprint_catalog",
        lambda _config: (SimpleNamespace(__str__=lambda _self: "/catalog"), [{"id": "worker-1"}]),
    )


def test_blueprint_run_forwards_secret_environment(monkeypatch):
    client, _runtime = _client(monkeypatch)
    blueprint = {"id": "worker-1", "name": "Worker", "installed": True}
    captured = {}
    monkeypatch.setattr(blueprints, "find_blueprint", lambda _config, _id: (None, blueprint))

    def resolve(_blueprint_id, request):
        captured["request"] = request
        return request

    monkeypatch.setattr(blueprints.legacy_blueprints, "resolve_async_blueprint_run_request", resolve)
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "run_blueprint_record",
        lambda *_args, **_kwargs: {"job_id": "job-blueprint", "run_id": "run-blueprint", "status": "pending"},
    )

    response = client.post(
        "/api/v1/blueprints/worker-1/runs",
        headers={"Idempotency-Key": "blueprint-run-secret-1"},
        json={
            "secret_environment": {"DECLARED_SECRET": "secret-value"},
            "owner_node": "mirror_neuron@spark",
        },
    )

    assert response.status_code == 202
    secret = captured["request"].secret_environment["DECLARED_SECRET"]
    assert secret.get_secret_value() == "secret-value"
    assert captured["request"].owner_node == "mirror_neuron@spark"
    assert "secret-value" not in response.text


def test_async_blueprint_requests_preserve_federated_owner_node():
    run_request = blueprints.legacy_blueprints.resolve_async_blueprint_run_request(
        "worker-1",
        blueprints.BlueprintRunRequest(owner_node="mirror_neuron@spark"),
    )
    launch_request = blueprints.legacy_blueprints.resolve_async_blueprint_launch_request(
        blueprints.legacy_blueprints.BlueprintLaunchRequest(
            source="catalog",
            blueprint_id="worker-1",
            owner_node="mirror_neuron@spark",
        )
    )

    assert run_request.owner_node == "mirror_neuron@spark"
    assert launch_request.owner_node == "mirror_neuron@spark"


def _wait_for_operation(client: TestClient, operation_id: str, terminal: set[str] | None = None) -> dict:
    terminal = terminal or {"completed", "failed"}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/v1/operations/{operation_id}").json()
        if snapshot.get("status") in terminal:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not reach {sorted(terminal)}")


def test_blueprint_addition_exposes_real_progress_result_and_local_sse(monkeypatch):
    client, _runtime = _client(monkeypatch)
    started = Event()
    release = Event()

    def add(_config, blueprint_id, *, report_progress, **_kwargs):
        report_progress(
            percent=55,
            stage="prepare_runtime",
            label="Prepare runtime prerequisites",
            detail="Preparing required runtime assets.",
        )
        started.set()
        assert release.wait(2)
        report_progress(
            percent=90,
            stage="record_addition",
            label="Record blueprint",
            detail="Recording the added blueprint.",
        )
        return {"added": True, "blueprint": {"id": blueprint_id, "added": True}}

    monkeypatch.setattr(blueprints, "add_catalog_blueprint", add)
    response = client.post(
        "/api/v1/blueprints/worker-1/additions",
        headers={"Idempotency-Key": "worker-1-add-progress"},
        json={},
    )
    assert response.status_code == 202
    operation_id = response.json()["operation_id"]
    assert started.wait(1)

    running = client.get(f"/api/v1/operations/{operation_id}").json()
    assert running["kind"] == "add_blueprint"
    assert running["status"] == "running"
    assert running["progress"] == {
        "percent": 55,
        "stage": "prepare_runtime",
        "label": "Prepare runtime prerequisites",
        "detail": "Preparing required runtime assets.",
    }

    release.set()
    completed = _wait_for_operation(client, operation_id)
    assert completed["status"] == "completed"
    assert completed["progress"]["percent"] == 100
    assert completed["result"]["blueprint"] == {"id": "worker-1", "added": True}

    stream = client.get(f"/api/v1/operations/{operation_id}/events/stream")
    assert stream.status_code == 200
    assert "event: operation.progress" in stream.text
    assert "event: operation.completed" in stream.text


def test_blueprint_addition_exposes_structured_sanitized_failure(monkeypatch):
    client, _runtime = _client(monkeypatch)

    def fail(_config, _blueprint_id, *, report_progress, **_kwargs):
        report_progress(
            percent=50,
            stage="prepare_runtime",
            label="Prepare runtime prerequisites",
            detail="Preparing required runtime assets.",
        )
        raise BlueprintAddError(
            issues=[
                {
                    "code": "runtime_model_not_ready",
                    "message": "model-one could not be prepared for this blueprint.",
                    "severity": "error",
                }
            ]
        )

    monkeypatch.setattr(blueprints, "add_catalog_blueprint", fail)
    response = client.post("/api/v1/blueprints/worker-1/additions", json={})
    operation = _wait_for_operation(client, response.json()["operation_id"])

    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "MN_BLUEPRINT_ADD_FAILED"
    assert operation["error"]["retryable"] is True
    assert operation["error"]["errors"][0]["code"] == "runtime_model_not_ready"
    assert "traceback" not in json.dumps(operation).lower()


def test_canonical_resource_happy_paths(monkeypatch):
    client, runtime = _client(monkeypatch)
    _patch_canonical_projections(monkeypatch)

    for path in (
        "/api/v1/runtime/status",
        "/api/v1/runtime/health",
        "/api/v1/runtime/diagnostics",
        "/api/v1/runtime/resources",
        "/api/v1/system/summary",
        "/api/v1/metrics",
        "/api/v1/nodes",
        "/api/v1/blueprints",
        "/api/v1/blueprints/worker-1",
        "/api/v1/runs",
        "/api/v1/jobs/job-1/runs",
        "/api/v1/runs/run-1",
        "/api/v1/models",
        "/api/v1/models/model-1",
        "/api/v1/model-remotes",
        "/api/v1/services",
        "/api/v1/services/llm/resolution",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)

    assert client.put("/api/v1/runtime/resources", json={"cpu": 8, "memory_mb": 4096}).status_code == 200
    assert client.post("/api/v1/nodes", json={"host": "node-2", "token": "join-token"}).status_code == 201
    assert client.put("/api/v1/nodes/node-1/drain", json={}).status_code == 202
    assert client.delete("/api/v1/nodes/node-1/drain").status_code == 204
    assert client.patch("/api/v1/nodes/node-1", json={"maintenance": True}).status_code == 200
    assert client.post("/api/v1/nodes/node-1/reconciliations", json={}).status_code == 202
    assert client.delete("/api/v1/nodes/node-1").status_code == 204

    addition = client.post(
        "/api/v1/blueprints/worker-1/additions",
        headers={"Idempotency-Key": "add-1"},
        json={},
    )
    assert addition.status_code == 202
    assert addition.headers["location"].startswith("/api/v1/operations/op-local-")
    assert client.get("/api/v1/blueprints/worker-1/installation").status_code == 404
    assert client.delete("/api/v1/blueprints/worker-1/installation").status_code == 404
    removal = client.post(
        "/api/v1/blueprints/worker-1/removals",
        headers={"Idempotency-Key": "remove-1"},
        json={"keep_resources": True},
    )
    assert removal.status_code == 202
    removed = _wait_for_operation(client, removal.json()["operation_id"])
    assert removed["result"] == {"removed": True, "blueprint_id": "worker-1"}
    assert client.post("/api/v1/blueprints/worker-1/validations", json={}).status_code == 201
    assert client.post(
        "/api/v1/blueprints/worker-1/runs",
        headers={"Idempotency-Key": "blueprint-run-1"},
        json={},
    ).status_code == 202
    refresh = client.post("/api/v1/blueprint-catalog-refreshes", headers={"Idempotency-Key": "refresh-1"})
    assert refresh.status_code == 202
    refresh_operation_id = refresh.json()["operation_id"]
    refresh_stream = client.get(f"/api/v1/operations/{refresh_operation_id}/events/stream")
    assert "event: operation.completed" in refresh_stream.text
    assert client.post("/api/v1/blueprint-cleanups", headers={"Idempotency-Key": "cleanup-1"}, json={}).status_code == 202

    created = client.post("/api/v1/jobs", json={"bundle_id": "bundle-1"})
    assert created.status_code == 201
    current = client.get("/api/v1/jobs/job-1")
    assert client.put(
        "/api/v1/jobs/job-1/bundle",
        headers={"If-Match": current.headers["etag"]},
        json={"bundle_id": "bundle-2"},
    ).status_code == 200
    assert client.post("/api/v1/jobs/job-1/data-resets").status_code == 202
    assert client.post("/api/v1/jobs/job-1/schedules", json={"schedule": {"kind": "cron"}}).status_code == 201

    runtime.run_status = "running"
    assert client.patch("/api/v1/runs/run-1", json={"desired_state": "paused"}).status_code == 200
    assert client.patch("/api/v1/runs/run-1", json={"desired_state": "running"}).status_code == 200
    assert client.patch("/api/v1/runs/run-1", json={"desired_state": "cancelled"}).status_code == 200
    assert client.delete("/api/v1/runs/run-1").status_code == 204


def test_run_artifact_projection_uses_runtime_id_from_result_reference(monkeypatch):
    client, runtime = _client(monkeypatch)
    _patch_canonical_projections(monkeypatch)
    runtime.get_run = lambda run_id: json.dumps(
        {
            "job_id": "job-1",
            "run_id": run_id,
            "status": "completed",
            "result_ref": {"run_id": "runtime-from-result-ref"},
        }
    )
    projected_ids: list[str] = []
    monkeypatch.setattr(
        jobs.runtime_run_routes,
        "get_run_final_artifact",
        lambda run_id, *_args: projected_ids.append(run_id) or {"artifact_id": "final"},
    )

    response = client.get("/api/v1/runs/run-1/artifacts/final")

    assert response.status_code == 200
    assert projected_ids == ["runtime-from-result-ref"]


def test_run_artifact_projection_resolves_staged_result_reference(monkeypatch, tmp_path):
    client, runtime = _client(monkeypatch)
    _patch_canonical_projections(monkeypatch)
    artifact = {"conversation_reply": {"reply": "Referenced result"}}
    encoded = json.dumps(artifact).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    relative_path = f"outputs/runs/runtime-from-result-ref/artifacts/{digest[:2]}/{digest}.json"
    shared_root = tmp_path / "shared"
    artifact_path = shared_root / "submissions" / "submission-1" / relative_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(encoded)
    monkeypatch.setenv("MN_HOST_SHARED_STORAGE_ROOT", str(shared_root))
    reference = {
        "version": "mn.staged_artifact/v1",
        "storage": "syncthing",
        "submission_id": "submission-1",
        "relative_path": relative_path,
        "sha256": digest,
        "size_bytes": len(encoded),
        "run_id": "runtime-from-result-ref",
    }
    runtime.get_run = lambda run_id: json.dumps(
        {
            "job_id": "job-1",
            "run_id": run_id,
            "status": "completed",
            "result_ref": reference,
        }
    )
    monkeypatch.setattr(
        jobs.runtime_run_routes,
        "get_run_final_artifact",
        lambda *_args: (_ for _ in ()).throw(
            jobs.HTTPException(status_code=404, detail="final artifact not found")
        ),
    )

    response = client.get("/api/v1/runs/run-1/artifacts/final")

    assert response.status_code == 200
    assert response.json()["conversation_reply"] == artifact["conversation_reply"]
    assert response.json()["run_id"] == "run-1"
    assert response.json()["runtime_run_id"] == "runtime-from-result-ref"


def test_run_monitor_overlays_canonical_terminal_status(monkeypatch):
    client, runtime = _client(monkeypatch)
    _patch_canonical_projections(monkeypatch)
    runtime.get_run = lambda run_id: json.dumps(
        {
            "job_id": "job-1",
            "run_id": run_id,
            "status": "completed",
            "runtime_run_id": "runtime-1",
        }
    )
    monkeypatch.setattr(
        jobs.runtime_job_routes,
        "_compact_job_detail",
        lambda _run_id: {
            "job": {"job_id": "runtime-1", "status": "unknown"},
            "summary": {"status": "unknown"},
        },
    )

    response = client.get("/api/v1/runs/run-1/monitor")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["job"]["status"] == "completed"
    assert response.json()["summary"]["status"] == "completed"


def test_canonical_run_detail_and_operations(monkeypatch):
    client, _runtime = _client(monkeypatch)
    _patch_canonical_projections(monkeypatch)

    for suffix in (
        "monitor",
        "workflow-progress",
        "logs",
        "events",
        "resources",
        "human-requests",
        "ui",
        "ui/video",
        "artifacts/final",
        "artifacts",
        "artifacts/report.txt",
        "outputs",
        "outputs/0",
        "observability",
        "snapshots",
        "agent-graph",
        "export",
    ):
        response = client.get(f"/api/v1/runs/run-1/{suffix}")
        assert response.status_code == 200, (suffix, response.text)

    assert client.post(
        "/api/v1/runs/run-1/human-requests/request-1/responses",
        json={"response": "approved"},
    ).status_code == 201
    assert client.post(
        "/api/v1/runs/run-1/human-requests/request-1/acknowledgements",
        json={"note": "seen"},
    ).status_code == 201
    stream = client.get("/api/v1/runs/run-1/events/stream?interval=0.25")
    assert stream.status_code == 200
    assert "event: run.completed" in stream.text

    assert client.put(
        "/api/v1/models/model-1/installation",
        headers={"Idempotency-Key": "model-install-1"},
        json={},
    ).status_code == 202
    assert client.delete("/api/v1/models/model-1/installation").status_code == 202
    assert client.post("/api/v1/models/model-1/benchmarks", json={"prompt": "OK"}).status_code == 201
    remote = client.post("/api/v1/model-remotes", json={"model": "m", "base_url": "http://model.local"})
    assert remote.status_code == 201
    assert client.delete("/api/v1/model-remotes/remote-1", headers={"If-Match": remote.headers["etag"]}).status_code == 204
    assert client.post("/api/v1/model-proxies", json={"model_id": "proxy-1"}).status_code == 201
