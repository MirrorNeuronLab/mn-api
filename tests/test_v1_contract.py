from __future__ import annotations

import hashlib
import json
from threading import Event
import time
from types import SimpleNamespace

import grpc
from fastapi import HTTPException
from fastapi.responses import JSONResponse
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

    def list_jobs(self, *, include_archived=False, page_size=50, page_token="", local_only=False):
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

    def get_job(self, job_id, *, include_workflow_definition=False):
        return json.dumps({"job_id": job_id, "status": "active", "revision": 1})

    def archive_job(self, job_id, **_kwargs):
        return json.dumps({"job_id": job_id, "status": "archived", "revision": 2})

    def delete_job(self, job_id, **_kwargs):
        self.calls.append(("delete_job", job_id))
        return json.dumps({"job_id": job_id, "deleted": True})

    def update_job(self, job_id, attrs, **kwargs):
        self.calls.append(("update_job", job_id, attrs, kwargs))
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
        self.calls.append(("create_job_schedule", job_id, _kwargs))
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

    def cancel_node_drain(self, node_id, **_kwargs):
        return json.dumps({"node": node_id, "draining": False})

    def remove_federated_peer(self, node_id):
        self.calls.append(("remove_federated_peer", node_id))
        return "removed"

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
    monkeypatch.setattr(jobs, "uploaded_bundle_root", lambda *_args: "/uploaded/package")
    monkeypatch.setattr(jobs, "local_blueprint_from_path", lambda _path: ("/uploaded", {"id": "g"}))
    monkeypatch.setattr(jobs, "load_blueprint_bundle", lambda *_args, **_kwargs: (manifest, {}))
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
        internal_jobs._decode_manifest(json.dumps({"apiVersion": "mn.workflow/unsupported"}))
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
        "/api/v1/runs/run-1/ui",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "not_found"


def test_job_ui_is_bound_to_the_durable_job(monkeypatch):
    client, _runtime = _client(monkeypatch)
    monkeypatch.setattr(
        jobs.runtime_job_routes,
        "get_job_ui",
        lambda job_id, *_args: {"job_id": job_id, "ui": {"job_id": job_id}, "web_ui": {"job_id": job_id}},
    )
    response = client.get("/api/v1/jobs/job-1/ui")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job-1",
        "ui": {"job_id": "job-1"},
        "web_ui": {"job_id": "job-1"},
    }


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

    replacement = client.post(
        "/api/v1/jobs/job-1/runs",
        json={
            "run_id": "run-replacement",
            "inputs": {"x": 3},
            "replace_existing_run": True,
        },
    )
    assert replacement.status_code == 202
    replacement_call = [call for call in runtime.calls if call[0] == "start_run"][-1]
    assert replacement_call[2]["replace_existing_run"] is True
    assert replacement_call[2]["run_id"] == "run-replacement"

    missing_replacement_id = client.post(
        "/api/v1/jobs/job-1/runs",
        json={"replace_existing_run": True},
    )
    assert missing_replacement_id.status_code == 422


def test_job_configuration_and_run_overrides_reprepare_catalog_definition(monkeypatch):
    client, runtime = _client(monkeypatch)
    prepared = []
    mappings = []
    relays = []
    output_relays = []

    monkeypatch.setattr(
        runtime,
        "get_job",
        lambda job_id: json.dumps({
            "job_id": job_id,
            "blueprint_id": "worker-1",
            "status": "active",
            "revision": 1,
            "owner_node": "mirror_neuron@gpu-node",
            "resolved_configuration": {"worker": {"mode": "saved"}},
            "native_resource_ownership": {"submission_id": "job-1-def-current"},
        }),
    )
    monkeypatch.setattr(
        jobs,
        "find_blueprint",
        lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}),
    )

    def prepare(root, blueprint, run_id, **kwargs):
        prepared.append((root, blueprint, run_id, kwargs))
        return '{"graph_id":"prepared-catalog"}', {"payload": b"ready"}

    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    monkeypatch.setattr(jobs, "write_blueprint_job_mapping", lambda *args, **kwargs: mappings.append((args, kwargs)))
    monkeypatch.setattr(
        jobs,
        "start_background_event_relay_if_needed",
        lambda *args, **kwargs: relays.append((args, kwargs)),
    )
    submission_lookups = []
    def load_submission(*args):
        submission_lookups.append(args)
        return {"output_copy": [{"source_path": "/runtime/output", "target_path": "/host/output"}]}
    monkeypatch.setattr(jobs.RuntimeConfig, "from_env", lambda: SimpleNamespace(shared_storage_root="/tmp/test-shared"))
    monkeypatch.setattr(jobs, "load_submission_storage", load_submission)
    monkeypatch.setattr(jobs, "start_background_run_relay", lambda *args, **kwargs: output_relays.append((args, kwargs)))

    current = client.get("/api/v1/jobs/job-1")
    updated = client.patch(
        "/api/v1/jobs/job-1",
        headers={"If-Match": current.headers["etag"]},
        json={"resolved_configuration": {"worker": {"mode": "updated"}}},
    )
    assert updated.status_code == 200, updated.text
    update_call = [call for call in runtime.calls if call[0] == "update_job"][-1]
    assert update_call[2] == {"resolved_configuration": {"worker": {"mode": "updated"}}}
    assert update_call[3]["manifest_json"] == '{"graph_id":"prepared-catalog"}'
    assert update_call[3]["payloads"] == {"payload": b"ready"}
    assert prepared[-1][3]["stable_job_id"] == "job-1"
    assert prepared[-1][3]["validate_inputs"] is True
    assert prepared[-1][3]["env_overrides"] == {
        "MN_SELECTED_RUNTIME_NODE": "mirror_neuron@gpu-node",
    }

    started = client.post(
        "/api/v1/jobs/job-1/runs",
        headers={"Idempotency-Key": "configured-start"},
        json={
            "inputs": {},
            "config_overrides": {"execution": {"quick_test": True}},
        },
    )
    assert started.status_code == 202, started.text
    run_update = [call for call in runtime.calls if call[0] == "update_job"][-1]
    assert run_update[2]["resolved_configuration"] == {
        "worker": {"mode": "saved"},
        "execution": {"quick_test": True},
    }
    assert prepared[-1][3]["validate_inputs"] is True
    assert prepared[-1][3]["env_overrides"] == {
        "MN_SELECTED_RUNTIME_NODE": "mirror_neuron@gpu-node",
    }
    start_call = [call for call in runtime.calls if call[0] == "start_run"][-1]
    assert start_call[2]["inputs"] == {}
    assert mappings[-1][0][1:] == ("job-1", "run-1")
    assert relays[-1][0][3] == "run-1"
    assert relays[-1][1]["config_overrides"]["execution"]["quick_test"] is True


    update_count = len([call for call in runtime.calls if call[0] == "update_job"])
    started_without_overrides = client.post(
        "/api/v1/jobs/job-1/runs",
        headers={"Idempotency-Key": "default-start"},
        json={"inputs": {}},
    )
    assert started_without_overrides.status_code == 202, started_without_overrides.text
    assert prepared[-1][3]["validate_inputs"] is True
    assert len([call for call in runtime.calls if call[0] == "update_job"]) == update_count
    assert len(prepared) == 2
    assert len(mappings) == 1
    assert len(relays) == 1
    assert submission_lookups[-1] == ("/tmp/test-shared", "job-1-def-current")
    assert output_relays[-1][0][0:2] == ("run-1", "run-1")
    assert output_relays[-1][0][2]["output_copy"][0]["target_path"] == "/host/output"

    def unexpected_preparation(*args, **kwargs):
        raise AssertionError("An unchanged prepared Job must not access the catalog or prepare placement")

    monkeypatch.setattr(jobs, "find_blueprint", unexpected_preparation)
    monkeypatch.setattr(jobs, "load_blueprint_bundle", unexpected_preparation)
    repeated = client.post(
        "/api/v1/jobs/job-1/runs",
        headers={"Idempotency-Key": "same-settings-start"},
        json={"inputs": {}, "config_overrides": {"worker": {"mode": "saved"}}},
    )
    assert repeated.status_code == 202, repeated.text
    assert len([call for call in runtime.calls if call[0] == "update_job"]) == update_count


def test_otterdesk_stable_service_start_acknowledges_run_with_runtime_shared_storage(monkeypatch):
    client, runtime = _client(monkeypatch)
    monkeypatch.setattr(
        runtime,
        "get_job",
        lambda job_id: json.dumps({
            "job_id": job_id,
            "blueprint_id": "cctv_operator",
            "status": "active",
            "revision": 1,
            "resolved_configuration": {"input": "sample"},
            "native_resource_ownership": {"submission_id": "job-co-definition"},
        }),
    )
    monkeypatch.setattr(jobs.RuntimeConfig, "from_env", lambda: SimpleNamespace(shared_storage_root="/runtime/shared"))
    storage_calls = []
    relay_calls = []

    def load_storage(root, submission_id):
        storage_calls.append((root, submission_id))
        return {"output_copy": [{"target_path": "/host/output"}]}

    monkeypatch.setattr(jobs, "load_submission_storage", load_storage)
    monkeypatch.setattr(jobs, "start_background_run_relay", lambda *args, **kwargs: relay_calls.append((args, kwargs)))
    monkeypatch.setattr(jobs, "find_blueprint", lambda *_args: (_ for _ in ()).throw(
        AssertionError("An unchanged Job must use its prepared definition")
    ))
    headers = {"Idempotency-Key": "otterdesk-sample-start"}
    body = {"inputs": {}, "config_overrides": {"input": "sample"}}

    started = client.post("/api/v1/jobs/job-co/runs", headers=headers, json=body)
    assert started.status_code == 202, started.text
    assert started.json()["run_id"] == "run-1"
    assert storage_calls == [("/runtime/shared", "job-co-definition")]
    assert relay_calls[0][0][:2] == ("run-1", "run-1")

    replay = client.post("/api/v1/jobs/job-co/runs", headers=headers, json=body)
    assert replay.status_code == 202, replay.text
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["run_id"] == "run-1"
    assert sum(call[0] == "start_run" for call in runtime.calls) == 1
    assert len(relay_calls) == 1


def test_run_uses_current_job_revision_after_bundle_preparation(monkeypatch):
    client, runtime = _client(monkeypatch)
    revision = 1

    def get_job(job_id, **_kwargs):
        return json.dumps({
            "job_id": job_id, "blueprint_id": "worker-1", "status": "active",
            "revision": revision, "resolved_configuration": {"worker": {"mode": "saved"}},
        })

    def prepare(*_args, **_kwargs):
        nonlocal revision
        revision += 1  # A background Job update completes during bundle preparation.
        return '{"graph_id":"prepared-catalog"}', {}

    monkeypatch.setattr(runtime, "get_job", get_job)
    monkeypatch.setattr(jobs, "find_blueprint", lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}))
    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    monkeypatch.setattr(jobs, "write_blueprint_job_mapping", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "start_background_event_relay_if_needed", lambda *_args, **_kwargs: None)

    started = client.post(
        "/api/v1/jobs/job-1/runs",
        json={"inputs": {}, "config_overrides": {"worker": {"mode": "updated"}}},
    )
    assert started.status_code == 202, started.text
    update_call = next(call for call in runtime.calls if call[0] == "update_job")
    assert update_call[3]["expected_revision"] == 2


def test_run_accepts_matching_configuration_saved_during_preparation(monkeypatch):
    client, runtime = _client(monkeypatch)
    configuration = {"worker": {"mode": "saved"}}
    cleaned = []
    mappings = []

    def get_job(job_id, **_kwargs):
        return json.dumps({
            "job_id": job_id, "blueprint_id": "worker-1", "status": "active",
            "revision": 2 if configuration["worker"]["mode"] == "updated" else 1,
            "resolved_configuration": configuration,
        })

    def prepare(*_args, **_kwargs):
        nonlocal configuration
        configuration = {"worker": {"mode": "updated"}}
        return '{"graph_id":"prepared-catalog"}', {}

    monkeypatch.setattr(runtime, "get_job", get_job)
    monkeypatch.setattr(jobs, "find_blueprint", lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}))
    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    monkeypatch.setattr(jobs, "cleanup_blueprint_run_processes", lambda *args, **kwargs: cleaned.append((args, kwargs)))
    monkeypatch.setattr(jobs, "write_blueprint_job_mapping", lambda *args, **kwargs: mappings.append((args, kwargs)))

    started = client.post(
        "/api/v1/jobs/job-1/runs",
        json={"inputs": {}, "config_overrides": {"worker": {"mode": "updated"}}},
    )
    assert started.status_code == 202, started.text
    assert [call[0] for call in runtime.calls].count("update_job") == 0
    assert [call[0] for call in runtime.calls].count("start_run") == 1
    assert cleaned[0][1]["reason"] == "redundant_preparation"
    assert mappings == []


def test_repeated_job_config_patch_reuses_prepared_definition(monkeypatch):
    client, runtime = _client(monkeypatch)
    monkeypatch.setattr(runtime, "get_job", lambda job_id, **_kwargs: json.dumps({
        "job_id": job_id, "blueprint_id": "cctv_operator", "status": "active",
        "revision": 7, "resolved_configuration": {"input": "sample"},
    }))
    monkeypatch.setattr(jobs, "find_blueprint", lambda *_args: (_ for _ in ()).throw(
        AssertionError("An unchanged configuration must not load the catalog")
    ))
    current = client.get("/api/v1/jobs/job-co")
    repeated = client.patch(
        "/api/v1/jobs/job-co",
        headers={"If-Match": current.headers["etag"]},
        json={"resolved_configuration": {"input": "sample"}},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["revision"] == 7
    assert repeated.headers["etag"] == current.headers["etag"]
    assert not any(call[0] == "update_job" for call in runtime.calls)

    renamed = client.patch(
        "/api/v1/jobs/job-co",
        headers={"If-Match": current.headers["etag"]},
        json={"display_name": "Camera", "resolved_configuration": {"input": "sample"}},
    )
    assert renamed.status_code == 200, renamed.text
    update = next(call for call in runtime.calls if call[0] == "update_job")
    assert update[2] == {"display_name": "Camera"}
    assert "manifest_json" not in update[3]


def test_otterdesk_config_patch_can_finish_while_run_prepares(monkeypatch):
    client, runtime = _client(monkeypatch)
    job = {
        "job_id": "job-1", "blueprint_id": "worker-1", "status": "active",
        "revision": 1, "resolved_configuration": {"worker": {"mode": "saved"}},
    }
    prepared = []
    cleaned = []

    monkeypatch.setattr(runtime, "get_job", lambda *_args, **_kwargs: json.dumps(job))

    def update_job(_job_id, attrs, **kwargs):
        assert kwargs["expected_revision"] == job["revision"]
        job.update(attrs)
        job["revision"] += 1
        runtime.calls.append(("update_job", attrs))
        return json.dumps(job)

    def prepare(*_args, **_kwargs):
        prepared.append(True)
        if len(prepared) == 1:
            current = client.get("/api/v1/jobs/job-1")
            synced = client.patch(
                "/api/v1/jobs/job-1",
                headers={"If-Match": current.headers["etag"]},
                json={"resolved_configuration": {"worker": {"mode": "updated"}}},
            )
            assert synced.status_code == 200, synced.text
        return '{"graph_id":"prepared-catalog"}', {}

    monkeypatch.setattr(runtime, "update_job", update_job)
    monkeypatch.setattr(jobs, "find_blueprint", lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}))
    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    monkeypatch.setattr(jobs, "cleanup_blueprint_run_processes", lambda *args, **kwargs: cleaned.append((args, kwargs)))

    started = client.post(
        "/api/v1/jobs/job-1/runs",
        json={"inputs": {}, "config_overrides": {"worker": {"mode": "updated"}}},
    )
    assert started.status_code == 202, started.text
    assert len(prepared) == 2
    assert len([call for call in runtime.calls if call[0] == "update_job"]) == 1
    assert len([call for call in runtime.calls if call[0] == "start_run"]) == 1
    assert cleaned[0][1]["reason"] == "redundant_preparation"


def test_job_config_patch_rejects_a_concurrent_different_save(monkeypatch):
    client, runtime = _client(monkeypatch)
    no_raise_client = TestClient(client.app, raise_server_exceptions=False)
    job = {
        "job_id": "job-1", "blueprint_id": "worker-1", "status": "active",
        "revision": 1, "resolved_configuration": {"worker": {"mode": "saved"}},
    }
    prepared = []

    class RevisionMismatch(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.ABORTED

        def details(self):
            return "revision_mismatch"

    monkeypatch.setattr(runtime, "get_job", lambda *_args, **_kwargs: json.dumps(job))

    def update_job(_job_id, attrs, **kwargs):
        if kwargs["expected_revision"] != job["revision"]:
            raise RevisionMismatch()
        job.update(attrs)
        job["revision"] += 1
        return json.dumps(job)

    def prepare(*_args, **_kwargs):
        prepared.append(True)
        if len(prepared) == 1:
            current = client.get("/api/v1/jobs/job-1")
            winner = client.patch(
                "/api/v1/jobs/job-1",
                headers={"If-Match": current.headers["etag"]},
                json={"resolved_configuration": {"worker": {"mode": "winner"}}},
            )
            assert winner.status_code == 200, winner.text
        return '{"graph_id":"prepared-catalog"}', {}

    monkeypatch.setattr(runtime, "update_job", update_job)
    monkeypatch.setattr(jobs, "find_blueprint", lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}))
    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)

    initial = no_raise_client.get("/api/v1/jobs/job-1")
    loser = no_raise_client.patch(
        "/api/v1/jobs/job-1",
        headers={"If-Match": initial.headers["etag"]},
        json={"resolved_configuration": {"worker": {"mode": "loser"}}},
    )
    assert loser.status_code == 409
    assert loser.headers["content-type"] == "application/problem+json"
    assert loser.json()["code"] == "MN_REVISION_CONFLICT"
    assert "revision_mismatch" not in loser.json()["detail"]
    assert job["resolved_configuration"] == {"worker": {"mode": "winner"}}
    assert job["revision"] == 2


def test_run_rejects_different_configuration_saved_during_preparation(monkeypatch):
    client, runtime = _client(monkeypatch)
    configuration = {"worker": {"mode": "saved"}}
    cleaned = []

    def get_job(job_id, **_kwargs):
        return json.dumps({
            "job_id": job_id, "blueprint_id": "worker-1", "status": "active",
            "revision": 2 if configuration["worker"]["mode"] == "other" else 1,
            "resolved_configuration": configuration,
        })

    def prepare(*_args, **_kwargs):
        nonlocal configuration
        configuration = {"worker": {"mode": "other"}}
        return '{"graph_id":"prepared-catalog"}', {}

    monkeypatch.setattr(runtime, "get_job", get_job)
    monkeypatch.setattr(jobs, "find_blueprint", lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}))
    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    monkeypatch.setattr(jobs, "cleanup_blueprint_run_processes", lambda *args, **kwargs: cleaned.append((args, kwargs)))

    rejected = client.post(
        "/api/v1/jobs/job-1/runs",
        json={"inputs": {}, "config_overrides": {"worker": {"mode": "updated"}}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "Job configuration changed during run preparation."
    assert [call[0] for call in runtime.calls].count("start_run") == 0
    assert cleaned[0][1]["reason"] == "launch_conflict"


def test_otterdesk_schedule_creation_retries_are_idempotent(monkeypatch):
    client, runtime = _client(monkeypatch)
    headers = {"Idempotency-Key": "otterdesk-schedule-1"}
    payload = {"schedule": {"kind": "periodic", "crons": ["0 9 * * *"]}}

    first = client.post("/api/v1/jobs/job-1/schedules", headers=headers, json=payload)
    replay = client.post("/api/v1/jobs/job-1/schedules", headers=headers, json=payload)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert replay.headers["idempotency-replayed"] == "true"
    creates = [call for call in runtime.calls if call[0] == "create_job_schedule"]
    assert len(creates) == 1
    assert creates[0][2]["idempotency_key"] == headers["Idempotency-Key"]

    conflict = client.post(
        "/api/v1/jobs/job-1/schedules",
        headers=headers,
        json={"schedule": {"kind": "periodic", "crons": ["0 10 * * *"]}},
    )
    assert conflict.status_code == 409
    assert len([call for call in runtime.calls if call[0] == "create_job_schedule"]) == 1


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
        "_workflow_progress_snapshot_for_run",
        lambda _id: {"run_id": "runtime-1", "status": "completed", "steps": []},
    )
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_logs", lambda *_args: {"data": [{"id": "log-1"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", lambda *_args: {"data": [{"id": "event-1", "type": "done"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_resources", lambda *_args: {"cpu": []})
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_human_events", lambda *_args: {"data": [{"request_id": "request-1"}]})
    monkeypatch.setattr(jobs.runtime_run_routes, "post_run_human_response", lambda *_args: {"request_id": "request-1", "status": "answered"})
    monkeypatch.setattr(jobs.runtime_run_routes, "post_run_human_ack", lambda *_args: {"request_id": "request-1", "status": "acknowledged"})
    monkeypatch.setattr(
        jobs.runtime_job_routes,
        "get_job_ui",
        lambda job_id, *_args: {"job_id": job_id, "ui": {"job_id": job_id}, "web_ui": {"job_id": job_id}},
    )
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


def test_blueprint_run_preserves_launch_error_response(monkeypatch):
    client, _runtime = _client(monkeypatch)
    blueprint = {"id": "worker-1", "name": "Worker", "installed": True}
    monkeypatch.setattr(blueprints, "find_blueprint", lambda _config, _id: (None, blueprint))
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "resolve_async_blueprint_run_request",
        lambda _blueprint_id, request: request,
    )
    monkeypatch.setattr(
        blueprints.legacy_blueprints,
        "run_blueprint_record",
        lambda *_args, **_kwargs: JSONResponse(
            status_code=409,
            content={"detail": "Selected owner is unavailable."},
        ),
    )

    response = client.post(
        "/api/v1/blueprints/worker-1/runs",
        headers={"Idempotency-Key": "blueprint-run-error-1"},
        json={},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Selected owner is unavailable."}
    assert "location" not in response.headers


def test_job_run_reports_scheduler_rejection_without_accepting_a_run(monkeypatch):
    _client_with_raised_exceptions, runtime = _client(monkeypatch)
    client = TestClient(create_app(), raise_server_exceptions=False)

    class AdmissionError(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.INTERNAL

        def details(self):
            return "resource_overloaded: memory is busy"

    def reject_run(*_args, **_kwargs):
        raise AdmissionError()

    monkeypatch.setattr(runtime, "start_run", reject_run)
    response = client.post(
        "/api/v1/jobs/job-1/runs",
        headers={"Idempotency-Key": "busy-run-1"},
        json={"inputs": {}},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "MN_RESOURCE_EXHAUSTED"
    assert "location" not in response.headers


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


def test_hardware_preflight_selects_unique_owner_node():
    selected = blueprints.legacy_blueprints.single_hardware_owner_node(
        {
            "results": [
                {
                    "type": "hardware_requirement",
                    "requirement": {"resource": "gpu", "enforcement": "hard"},
                    "matching_nodes": ["mirror_neuron@spark"],
                }
            ]
        }
    )

    assert selected == "mirror_neuron@spark"


def test_blueprint_output_relay_polls_execution_run_id(monkeypatch, tmp_path):
    legacy = blueprints.legacy_blueprints
    bundle = tmp_path / "worker-1"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    captured = {}

    class Runtime:
        def create_job(self, _manifest, _payloads, **kwargs):
            captured["owner_node"] = kwargs.get("owner_node")
            return json.dumps({"job_id": "job-stable"})

        def start_run(self, job_id, **_kwargs):
            assert job_id == "job-stable"
            return json.dumps({"run_id": "run-execution"})

    monkeypatch.setattr(legacy, "record_launch_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(legacy, "validate_progress_id", lambda value: value)
    monkeypatch.setattr(legacy, "validate_blueprint_bundle", lambda *_args: bundle)
    monkeypatch.setattr(legacy, "read_manifest_for_launch", lambda _bundle: {})
    monkeypatch.setattr(legacy, "validate_blueprint_secret_environment", lambda *_args: None)
    monkeypatch.setattr(legacy, "runtime_blueprint_environment_overrides", lambda: {})
    monkeypatch.setattr(legacy, "fake_mode_environment_overrides", lambda _req: {})
    monkeypatch.setattr(legacy.state, "close_client", lambda: None)
    monkeypatch.setattr(
        legacy,
        "run_launch_preflight",
        lambda *_args, **_kwargs: SimpleNamespace(
            model_install=None,
            env_overrides={},
            config_overrides={},
        ),
    )
    monkeypatch.setattr(legacy, "runtime_active_job_ids", lambda: set())
    monkeypatch.setattr(legacy, "cleanup_stale_blueprint_run_processes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(legacy, "start_blueprint_pre_launch_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(legacy, "generate_stable_job_id", lambda _blueprint_id: "job-requested")
    monkeypatch.setattr(legacy, "generate_job_definition_submission_id", lambda _job_id: "submission-1")
    monkeypatch.setattr(
        legacy,
        "load_blueprint_bundle",
        lambda *_args, **_kwargs: (
            json.dumps(
                {
                    "metadata": {
                        "mn_docker_workers": {
                            "services": [
                                {
                                    "node": "mirror_neuron@spark",
                                    "service": "cctv-adaptive-frame-sampler",
                                }
                            ]
                        }
                    }
                }
            ),
            {},
        ),
    )
    monkeypatch.setattr(legacy, "inject_declared_secret_environment", lambda manifest, _secrets: manifest)
    monkeypatch.setattr(legacy.state, "client", Runtime())
    monkeypatch.setattr(legacy, "write_blueprint_job_mapping", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(legacy, "manifest_without_secret_environment", lambda manifest, _secrets: manifest)
    monkeypatch.setattr(legacy, "_current_config", lambda: SimpleNamespace())

    def capture_relay(_repo, _blueprint, run_id, execution_id, _manifest, **_kwargs):
        captured.update(run_id=run_id, execution_id=execution_id)

    monkeypatch.setattr(legacy, "start_background_event_relay_if_needed", capture_relay)

    result = legacy.run_blueprint_record(
        tmp_path,
        {"id": "worker-1", "revision": "abc"},
        legacy.BlueprintRunRequest(run_id="run-requested", force=True),
    )

    assert result["job_id"] == "job-stable"
    assert result["run_id"] == "run-execution"
    assert captured == {
        "owner_node": "mirror_neuron@spark",
        "run_id": "run-requested",
        "execution_id": "run-execution",
    }


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
    removed_node = client.delete("/api/v1/nodes/node-2")
    assert removed_node.status_code == 200
    assert removed_node.json() == {"node_name": "node-2", "status": "removed"}
    assert ("remove_federated_peer", "node-2") in runtime.calls
    assert client.put("/api/v1/nodes/node-1/drain", json={}).status_code == 202
    assert client.delete("/api/v1/nodes/node-1/drain").status_code == 204
    assert client.patch("/api/v1/nodes/node-1", json={"maintenance": True}).status_code == 200
    assert client.post("/api/v1/nodes/node-1/reconciliations", json={}).status_code == 202
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
        json={"bundle_id": "bundle-2", "replace_existing_run": True},
    ).status_code == 200
    bundle_update = [call for call in runtime.calls if call[0] == "update_job"][-1]
    assert bundle_update[3]["replace_existing_run"] is True
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
    monitored_ids = []
    runtime.get_run = lambda run_id: json.dumps(
        {
            "job_id": "job-1",
            "run_id": run_id,
            "status": "completed",
            "runtime_run_id": "runtime-1",
        }
    )
    def monitor_for_run(run_id):
        monitored_ids.append(run_id)
        return {
            "job": {"job_id": "runtime-1", "status": "unknown"},
            "summary": {"status": "unknown"},
        }

    monkeypatch.setattr(jobs.runtime_job_routes, "_compact_job_detail", monitor_for_run)

    response = client.get("/api/v1/runs/run-1/monitor")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["job"]["status"] == "completed"
    assert response.json()["summary"]["status"] == "completed"
    assert monitored_ids == ["run-1"]


def test_run_progress_uses_execution_id_when_output_id_differs(monkeypatch):
    client, runtime = _client(monkeypatch)
    runtime.get_run = lambda run_id: json.dumps({
        "job_id": "job-1", "run_id": run_id, "runtime_run_id": "output-1", "status": "completed"
    })
    progress_ids = []
    event_ids = []

    def progress_for_run(run_id):
        progress_ids.append(run_id)
        return {
            "job_id": run_id,
            "status": "completed",
            "completed_steps": 1,
            "total_steps": 1,
            "steps": [{"id": "prepare", "status": "done"}],
        }

    def events_for_run(run_id, *_args):
        event_ids.append(run_id)
        return {"items": []}

    monkeypatch.setattr(jobs.runtime_job_routes, "_workflow_progress_snapshot_for_run", progress_for_run)
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", events_for_run)

    response = client.get("/api/v1/runs/run-2/workflow-progress")
    assert response.status_code == 200
    assert response.json()["run_id"] == "run-2"
    assert response.json()["runtime_run_id"] == "output-1"
    assert response.json()["steps"][0]["status"] == "done"

    stream = client.get("/api/v1/runs/run-2/events/stream?interval=0.25")
    assert stream.status_code == 200
    assert '"run_id":"run-2"' in stream.text
    assert progress_ids == ["run-2", "run-2"]
    assert event_ids == ["run-2"]


def test_shared_run_events_use_mapped_run_id_before_core_result_reference(monkeypatch):
    client, runtime = _client(monkeypatch)
    runtime.get_run = lambda run_id: json.dumps({
        "run_id": run_id, "status": "failed", "result_ref": {"run_id": "bootstrap-old"},
    })
    monkeypatch.setattr(jobs, "shared_run_dir", lambda _run_id: object())
    received = []
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", lambda run_id, *_args: (
        received.append(run_id) or {"data": [{"type": "workflow_step_failed", "timestamp": "2026-01-01T00:00:00Z"}]}
    ))

    response = client.get("/api/v1/runs/run-1/events?page_size=2")

    assert response.status_code == 200
    assert received == ["run-1"]


def test_active_service_result_events_are_served_from_shared_submission(monkeypatch):
    client, runtime = _client(monkeypatch)
    runtime.get_run = lambda run_id: json.dumps({
        "run_id": run_id, "job_id": "job-microduck", "status": "running"
    })
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", lambda *_args: (
        (_ for _ in ()).throw(HTTPException(status_code=404, detail="run not found"))
    ))
    calls = []
    monkeypatch.setattr(jobs, "shared_result_events", lambda job_id, run_id: (
        calls.append((job_id, run_id)) or [{
            "type": "run_result_available", "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"run_id": run_id, "result_id": "aaaaaaaaaaaaaaaaaaaaaaaa", "kind": "web_ui"}
        }]
    ))

    response = client.get("/api/v1/runs/mcv-live/events")

    assert response.status_code == 200
    assert calls == [("job-microduck", "mcv-live")]
    assert response.json()["items"][0]["type"] == "run_result_available"


def test_run_event_stream_keeps_snapshot_when_local_event_copy_is_not_ready(monkeypatch):
    from fastapi import HTTPException

    client, runtime = _client(monkeypatch)
    runtime.get_run = lambda run_id: json.dumps({
        "job_id": "job-1", "run_id": run_id, "status": "completed"
    })
    monkeypatch.setattr(jobs.runtime_job_routes, "_workflow_progress_snapshot_for_run", lambda _id: {
        "job_id": "job-1", "status": "completed", "steps": [],
    })
    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_events", lambda *_args: (_ for _ in ()).throw(
        HTTPException(status_code=404, detail="run not found")
    ))

    response = client.get("/api/v1/runs/run-2/events/stream?interval=0.25")

    assert response.status_code == 200
    assert '"type":"run.snapshot"' in response.text


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


def test_uploaded_job_uses_catalog_preparation_before_runtime_submission(monkeypatch):
    client, runtime = _client(monkeypatch)
    prepared = []

    def prepare(root, blueprint, run_id, **kwargs):
        prepared.append((root, blueprint, run_id, kwargs))
        return '{"graph_id":"prepared-upload","flow":{"nodes":[]}}', {}

    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    response = client.post(
        "/api/v1/jobs",
        json={"bundle_id": "bundle-1", "job_id": "upload-job", "resolved_configuration": {"sample": 3}},
        headers={"Idempotency-Key": "upload-preparation"},
    )
    assert response.status_code == 201, response.text
    assert len(prepared) == 1
    root, blueprint, run_id, options = prepared[0]
    assert root == "/uploaded"
    assert blueprint == {"id": "g"}
    assert run_id
    assert options["stable_job_id"] == "upload-job"
    assert options["config_overrides"] == {"sample": 3}
    assert options["validate_inputs"] is True
    assert options["submission_id"]
    assert runtime.calls[-1][0] == "create_job"


def test_catalog_job_create_preserves_owner_with_definition_input_validation(monkeypatch):
    client, runtime = _client(monkeypatch)
    prepared = []

    monkeypatch.setattr(
        jobs,
        "find_blueprint",
        lambda _config, blueprint_id: ("/catalog", {"id": blueprint_id}),
    )

    def prepare(root, blueprint, run_id, **kwargs):
        prepared.append((root, blueprint, run_id, kwargs))
        return '{"graph_id":"prepared-catalog","flow":{"nodes":[]}}', {}

    monkeypatch.setattr(jobs, "load_blueprint_bundle", prepare)
    response = client.post(
        "/api/v1/jobs",
        json={
            "blueprint_id": "gpu-worker",
            "owner_node": "mirror_neuron@gpu-node",
        },
        headers={"Idempotency-Key": "catalog-owner-placement"},
    )

    assert response.status_code == 201, response.text
    assert len(prepared) == 1
    root, blueprint, run_id, options = prepared[0]
    assert root == "/catalog"
    assert blueprint == {"id": "gpu-worker"}
    assert run_id
    assert options["env_overrides"] == {
        "MN_SELECTED_RUNTIME_NODE": "mirror_neuron@gpu-node",
    }
    assert options["config_overrides"] == {}
    assert options["validate_inputs"] is True
    assert runtime.calls[-1][0] == "create_job"
    assert all(call[0] != "start_run" for call in runtime.calls)
    assert runtime.calls[-1][1]["owner_node"] == "mirror_neuron@gpu-node"
    assert runtime.calls[-1][1]["resolved_configuration"] == {}


def test_remote_operation_stream_replays_and_stops_at_terminal(monkeypatch):
    from mn_api.routes.v1 import operations

    client, runtime = _client(monkeypatch)
    monkeypatch.setattr(operations, "is_local_operation", lambda _: False)
    events = [
        {"type": "operation.started", "status": "running"},
        {"type": "operation.completed", "status": "completed"},
        {"type": "unexpected", "status": "running"},
    ]
    runtime.stream_operation_events = lambda *_args, **_kwargs: iter(map(json.dumps, events))
    response = client.get("/api/v1/operations/remote-replay/events/stream", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 2" in response.text
    assert "operation.completed" in response.text
    assert "operation.started" not in response.text
    assert "unexpected" not in response.text


def test_remote_operation_stream_emits_snapshot_when_upstream_ends(monkeypatch):
    from mn_api.routes.v1 import operations

    client, runtime = _client(monkeypatch)
    monkeypatch.setattr(operations, "is_local_operation", lambda _: False)
    runtime.stream_operation_events = lambda *_args, **_kwargs: iter([json.dumps({"status": "running"})])
    monkeypatch.setattr(operations, "get_operation", lambda operation_id: {"operation_id": operation_id, "status": "completed"})
    response = client.get("/api/v1/operations/remote-ended/events/stream")
    assert response.status_code == 200
    assert "id: 1" in response.text
    assert "id: 2" in response.text
    assert "operation.snapshot" in response.text
    assert '"status":"completed"' in response.text
    invalid = client.get("/api/v1/operations/remote-ended/events/stream", headers={"Last-Event-ID": "invalid"})
    assert invalid.status_code == 400
    assert invalid.headers["content-type"].startswith("application/problem+json")


def test_staged_final_artifact_errors_preserve_retry_and_integrity_contract(monkeypatch):
    client, _runtime = _client(monkeypatch)
    _patch_canonical_projections(monkeypatch)

    def missing(*_args):
        raise HTTPException(status_code=404, detail="final artifact not found")

    monkeypatch.setattr(jobs.runtime_run_routes, "get_run_final_artifact", missing)
    for error, status, code in [
        (jobs.ArtifactNotReadyError("artifact pending"), 503, "service_unavailable"),
        (jobs.ArtifactIntegrityError("digest mismatch"), 500, "internal_error"),
        (jobs.StagedArtifactError("invalid reference"), 500, "internal_error"),
    ]:
        def resolve(_run_id, failure=error):
            raise failure
        monkeypatch.setattr(jobs, "_resolve_run_result_reference", resolve)
        response = client.get("/api/v1/runs/run-1/artifacts/final")
        assert response.status_code == status
        assert response.json()["code"] == code
        assert str(error) not in response.text
        assert response.headers.get("retry-after") == ("1" if status == 503 else None)


def test_job_workflow_shape_views_and_progress_only(monkeypatch):
    client, runtime = _client(monkeypatch)
    definition = {
        "workflow": {
            "workflow_id": "flow-1",
            "steps": [
                {"id": "start", "label": "Start", "run": "start"},
                {"id": "left", "label": "Left", "run": "left"},
                {"id": "right", "label": "Right", "run": "right"},
            ],
            "edges": [{"from": "start", "to": "left"}, {"from": "start", "to": "right"}],
        },
        "runtime": {"bindings": {"left": {"workers": [{"id": "agent-left", "role": "research", "model": "m"}]}}},
    }
    runtime.get_job = lambda job_id, **_kwargs: json.dumps({
        "job_id": job_id,
        "status": "active",
        "revision": 1,
        "latest_run_id": "run-1",
        "workflow_definition": definition,
    })
    snapshot = {
        "job_id": "runtime-1",
        "workflow_id": "flow-1",
        "graph_revision": 2,
        "status": "completed",
        "steps": [
            {"id": "start", "label": "Start", "goal": "", "status": "done", "parents": [], "agents": []},
            {"id": "dynamic", "label": "Dynamic", "goal": "Inspect", "status": "done", "parents": ["start"],
             "agents": [{"id": "agent-dynamic", "display_name": "Inspector", "role": "inspect", "model": "m",
                         "status": "done", "progress": 1.0}]},
        ],
        "edges": [{"from": "start", "to": "dynamic"}],
        "layers": [["start"], ["dynamic"]],
        "current_step": None,
    }
    monkeypatch.setattr(jobs.runtime_job_routes, "_workflow_progress_snapshot_for_run", lambda _id: snapshot)

    base = "/api/v1/jobs/job-1/workflow"
    dag = client.get(f"{base}/definition/dag")
    assert dag.status_code == 200, dag.text
    assert dag.json()["layers"] == [["start"], ["left", "right"]]
    assert [node["id"] for node in dag.json()["nodes"]] == ["start", "left", "right"]
    steps = client.get(f"{base}/definition/steps")
    assert steps.status_code == 200, steps.text
    assert steps.json()["steps"][1]["agents"] == [
        {"id": "agent-left", "display_name": "", "role": "research", "model": "m"}
    ]
    latest = client.get(f"{base}/latest-run/steps")
    assert latest.status_code == 200, latest.text
    assert latest.json()["run_id"] == "run-1"
    assert latest.json()["steps"][1]["agents"][0]["id"] == "agent-dynamic"
    assert client.get(f"{base}/latest-run/dag").json()["edges"] == [{"from": "start", "to": "dynamic"}]
    assert "workflow_definition" not in client.get("/api/v1/jobs/job-1").json()

    progress = client.get("/api/v1/runs/run-1/workflow-progress")
    assert progress.status_code == 200, progress.text
    assert progress.json()["steps"][1]["status"] == "done"
    assert progress.json()["steps"][1]["agents"][0]["progress"] == 1.0
    assert "edges" not in progress.json()
    assert "layers" not in progress.json()
    assert "label" not in progress.json()["steps"][1]
    assert "role" not in progress.json()["steps"][1]["agents"][0]

    monkeypatch.setattr(jobs.runtime_job_routes, "_workflow_progress_snapshot_for_run", lambda _id: {
        "job_id": "runtime-1", "status": "running", "steps": [], "edges": [], "layers": []
    })
    empty_runtime_shape = client.get(f"{base}/latest-run/steps")
    assert empty_runtime_shape.status_code == 200
    assert [step["id"] for step in empty_runtime_shape.json()["steps"]] == ["start", "left", "right"]
    assert empty_runtime_shape.json()["run_id"] == "run-1"


def test_job_workflow_shape_missing_latest_run_and_empty_definition(monkeypatch):
    client, runtime = _client(monkeypatch)
    runtime.get_job = lambda job_id, **_kwargs: json.dumps({"job_id": job_id, "workflow_definition": {}})
    dag = client.get("/api/v1/jobs/job-1/workflow/definition/dag")
    assert dag.status_code == 200, dag.text
    assert dag.json()["nodes"] == []
    assert client.get("/api/v1/jobs/job-1/workflow/definition/steps").json()["steps"] == []
    missing = client.get("/api/v1/jobs/job-1/workflow/latest-run/dag")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
