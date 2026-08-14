from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.contracts import API_CONTRACT
from mn_api.http_semantics import strong_etag
from mn_api.pagination import PageTokenRegistry, page
from mn_api.routes.v1 import blueprints, infrastructure, jobs, system


class CanonicalRuntime:
    def __init__(self):
        self.calls: list[tuple] = []
        self.run_status = "completed"

    def create_stable_job(self, _manifest, _payloads, **kwargs):
        self.calls.append(("create_stable_job", kwargs))
        return json.dumps({"version": 2, "job_id": kwargs.get("job_id") or "job-1", "status": "active", "revision": 1})

    def list_stable_jobs(self, *, include_archived=False, page_size=50, page_token=""):
        self.calls.append(("list_stable_jobs", include_archived))
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

    def get_stable_job(self, job_id):
        return json.dumps({"job_id": job_id, "status": "active", "revision": 1})

    def archive_stable_job(self, job_id, **_kwargs):
        return json.dumps({"job_id": job_id, "status": "archived", "revision": 2})

    def delete_stable_job(self, job_id, **_kwargs):
        self.calls.append(("delete_stable_job", job_id))
        return json.dumps({"job_id": job_id, "deleted": True})

    def update_stable_job(self, job_id, attrs, **_kwargs):
        self.calls.append(("update_stable_job", job_id, attrs))
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
    manifest = '{"apiVersion":"mn.workflow/v2","graph_id":"g","nodes":[]}'
    monkeypatch.setattr(jobs, "load_uploaded_bundle", lambda *_args: (manifest, {}))
    monkeypatch.setattr(infrastructure, "load_uploaded_bundle", lambda *_args: (manifest, {}))
    return TestClient(create_app()), runtime


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
    for path in ("/api/v1/jobs", "/api/v1/runs", "/api/v1/schedules", "/api/v1/operations"):
        success_schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert success_schema["$ref"].endswith("/PageResponse")


def test_health_capability_and_removed_routes(monkeypatch):
    client, _runtime = _client(monkeypatch)
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "api_contract": API_CONTRACT, "auth": "disabled"}

    for path in ("/api/v2/health", "/api/v1/runtime-runs", "/api/v1/realtime"):
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
    monkeypatch.setattr(infrastructure.model_routes, "load_model_remotes", lambda: {"remotes": {"remote-1": {"name": "remote-1", "model": "m"}}})
    monkeypatch.setattr(infrastructure, "upsert_model_remote", lambda *_args, **_kwargs: {"name": "remote-1", "model": "m"})
    monkeypatch.setattr(infrastructure, "remove_model_remote", lambda _name: None)
    monkeypatch.setattr(infrastructure, "upsert_model_proxy", lambda *_args, **_kwargs: {"model_id": "proxy-1"})
    monkeypatch.setattr(infrastructure, "uploaded_bundle_root", lambda *_args: SimpleNamespace(__truediv__=lambda *_: None))
    monkeypatch.setattr(infrastructure, "run_service_validation", lambda *_args, **_kwargs: {"ok": True})


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
        "/api/v1/schedules",
        "/api/v1/trigger-events",
        "/api/v1/deployments",
        "/api/v1/deployments/deployment-1",
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

    installation = client.get("/api/v1/blueprints/worker-1/installation")
    conditional = {"If-Match": installation.headers["etag"], "Idempotency-Key": "install-1"}
    assert client.put("/api/v1/blueprints/worker-1/installation", headers=conditional, json={}).status_code == 202
    assert client.post("/api/v1/blueprints/worker-1/validations", json={}).status_code == 201
    assert client.post(
        "/api/v1/blueprints/worker-1/runs",
        headers={"Idempotency-Key": "blueprint-run-1"},
        json={},
    ).status_code == 202
    assert client.post("/api/v1/blueprint-catalog-refreshes", headers={"Idempotency-Key": "refresh-1"}).status_code == 202
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


def test_canonical_run_detail_operation_schedule_and_infrastructure(monkeypatch):
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

    for route, key in (("run-cleanups", "clear-1"), ("run-cancellations", "cancel-all-1")):
        assert client.post(f"/api/v1/{route}", headers={"Idempotency-Key": key}, json={}).status_code == 202
    listed = client.get("/api/v1/operations")
    assert listed.status_code == 200
    operation_id = listed.json()["items"][0]["operation_id"]
    assert client.get(f"/api/v1/operations/{operation_id}").status_code == 200
    operation_stream = client.get(f"/api/v1/operations/{operation_id}/events/stream")
    assert operation_stream.status_code == 200
    assert "event: operation.completed" in operation_stream.text

    schedule = client.get("/api/v1/schedules/schedule-1")
    match = {"If-Match": schedule.headers["etag"]}
    assert client.patch("/api/v1/schedules/schedule-1", headers=match, json={"desired_state": "paused"}).status_code == 200
    schedule = client.get("/api/v1/schedules/schedule-1")
    assert client.delete("/api/v1/schedules/schedule-1", headers={"If-Match": schedule.headers["etag"]}).status_code == 204
    assert client.post(
        "/api/v1/schedules/schedule-1/dispatches",
        headers={"Idempotency-Key": "dispatch-1"},
        json={},
    ).status_code == 202
    assert client.post("/api/v1/trigger-events", json={"event_type": "invoice.received"}).status_code == 201

    created_deployment = client.post("/api/v1/deployments", json={"bundle_id": "bundle-1", "deployment_key": "deployment-1"})
    assert created_deployment.status_code == 201
    deployment = client.get("/api/v1/deployments/deployment-1")
    assert client.patch(
        "/api/v1/deployments/deployment-1",
        headers={"If-Match": deployment.headers["etag"]},
        json={"desired_state": "paused"},
    ).status_code == 200
    assert client.post("/api/v1/deployments/deployment-1/promotions").status_code == 201
    assert client.post("/api/v1/deployments/deployment-1/rollbacks", json={}).status_code == 202

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
