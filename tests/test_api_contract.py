from __future__ import annotations

from types import SimpleNamespace
import warnings

from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.routes import (
    blueprints,
    bundles,
    deployments,
    jobs,
    jobs_v2,
    models,
    runs,
    schedules,
    services,
    system,
)
from tests.test_cli_parity_routes import API_COVERED_COMMANDS


def test_openapi_generates_without_duplicate_operation_warnings():
    app = create_app()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = app.openapi()

    duplicate_warnings = [warning for warning in caught if "Duplicate Operation ID" in str(warning.message)]
    assert duplicate_warnings == []
    operation_ids = [
        operation["operationId"]
        for methods in schema["paths"].values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_only_v2_http_api_is_exposed(api_client):
    paths = create_app().openapi()["paths"]

    assert paths
    assert all(path.startswith("/api/v2") for path in paths)
    assert api_client.get("/api/v1/health").status_code == 404
    assert api_client.get("/api/v2/health").status_code == 200


def test_v2_request_models_reject_interface_version_one(api_client):
    response = api_client.post(
        "/api/v2/jobs",
        json={
            "version": 1,
            "manifest_json": '{"apiVersion":"mn.workflow/v2","graph_id":"g","nodes":[]}',
        },
    )

    assert response.status_code == 422


def test_representative_protected_routes_require_bearer_token(monkeypatch, api_client, state_snapshot):
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(api_token="secret", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]),
    )

    for path in (
        "/api/v2/runtime/status",
        "/api/v2/jobs",
        "/api/v2/models",
        "/api/v2/services",
        "/api/v2/resource",
        "/api/v2/runs",
        "/api/v2/blueprints",
    ):
        response = api_client.get(path)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "missing or invalid bearer token"


def test_cli_parity_commands_map_to_existing_api_routes():
    route_paths = _api_route_paths()

    assert set(COMMAND_ENDPOINTS) == API_COVERED_COMMANDS
    missing = {
        command: path
        for command, path in COMMAND_ENDPOINTS.items()
        if path not in route_paths
    }
    assert missing == {}


def test_openapi_exposes_cleanup_and_service_check_aliases_with_distinct_ids():
    schema = create_app().openapi()

    assert schema["paths"]["/api/v2/jobs:cleanup"]["post"]["operationId"] == "cleanup_jobs_colon_alias"
    assert schema["paths"]["/api/v2/jobs/cleanup"]["post"]["operationId"] == "cleanup_jobs_path_alias"
    assert schema["paths"]["/api/v2/services:check"]["post"]["operationId"] == "check_services_colon_alias"
    assert schema["paths"]["/api/v2/services/check"]["post"]["operationId"] == "check_services_path_alias"


def test_valid_bearer_token_reaches_route(monkeypatch, state_snapshot):
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(
            api_token="secret",
            request_size_limit_bytes=1024 * 1024,
            cors_allow_origins=[],
            env="test",
            blueprint_source="local",
            blueprint_repo="",
            blueprint_local="",
            active_blueprint_location="",
        ),
    )

    response = TestClient(create_app()).get("/api/v2/health", headers={"authorization": "Bearer secret"})

    assert response.status_code == 200
    assert response.json()["auth"] == "enabled"


def _api_route_paths() -> set[str]:
    modules = (
        blueprints,
        bundles,
        deployments,
        jobs,
        jobs_v2,
        models,
        runs,
        schedules,
        services,
        system,
    )
    return {route.path for module in modules for route in module.router.routes}


COMMAND_ENDPOINTS = {
    "blueprint cleanup": "/api/v2/blueprints:cleanup",
    "blueprint compare": "/api/v2/runtime-runs:compare",
    "blueprint doctor": "/api/v2/blueprints/{blueprint_id}/validate",
    "blueprint export": "/api/v2/runtime-runs/{run_id}/export",
    "blueprint human": "/api/v2/runtime-runs/{run_id}/human",
    "blueprint human ack": "/api/v2/runtime-runs/{run_id}/human/{notice_id}/ack",
    "blueprint human respond": "/api/v2/runtime-runs/{run_id}/human/{request_id}/response",
    "blueprint install": "/api/v2/blueprints/{blueprint_id}/install",
    "blueprint list": "/api/v2/blueprints",
    "blueprint logs": "/api/v2/runtime-runs/{run_id}/logs",
    "blueprint monitor": "/api/v2/runtime-runs/ws",
    "blueprint resources": "/api/v2/runtime-runs/{run_id}/resources",
    "blueprint run": "/api/v2/blueprints/{blueprint_id}/runs",
    "blueprint stream": "/api/v2/runtime-runs/{run_id}/stream",
    "blueprint tail": "/api/v2/runtime-runs/{run_id}/events",
    "blueprint uninstall": "/api/v2/blueprints:uninstall",
    "blueprint update": "/api/v2/blueprints:update",
    "blueprint validate": "/api/v2/blueprints/{blueprint_id}/validate",
    "deployment deploy": "/api/v2/deployments",
    "deployment fail": "/api/v2/deployments/{id_or_key}/fail",
    "deployment list": "/api/v2/deployments",
    "deployment pause": "/api/v2/deployments/{id_or_key}/pause",
    "deployment promote": "/api/v2/deployments/{id_or_key}/promote",
    "deployment resume": "/api/v2/deployments/{id_or_key}/resume",
    "deployment rollback": "/api/v2/deployments/{id_or_key}/rollback",
    "deployment status": "/api/v2/deployments/{id_or_key}",
    "event emit": "/api/v2/events",
    "event list": "/api/v2/events",
    "job archive": "/api/v2/jobs/{job_id}/archive",
    "job backup": "/api/v2/jobs/{job_id}/backup",
    "job cancel": "/api/v2/jobs/{job_id}/cancel",
    "job cancel-all": "/api/v2/jobs:cancel-all",
    "job clear": "/api/v2/jobs:cleanup",
    "job create": "/api/v2/jobs",
    "job dead-letters": "/api/v2/jobs/{job_id}/dead-letters",
    "job definitions": "/api/v2/jobs",
    "job delete": "/api/v2/jobs/{job_id}",
    "job inspect": "/api/v2/jobs/{job_id}",
    "job list": "/api/v2/jobs",
    "job monitor": "/api/v2/runs/{run_id}/workflow-progress/stream",
    "job pause": "/api/v2/jobs/{job_id}/pause",
    "job reset-data": "/api/v2/jobs/{job_id}/data:reset",
    "job restore": "/api/v2/jobs/restore",
    "job result": "/api/v2/jobs/{job_id}",
    "job resume": "/api/v2/jobs/{job_id}/resume",
    "job runs": "/api/v2/jobs/{job_id}/runs",
    "job start": "/api/v2/jobs/{job_id}/runs",
    "job status": "/api/v2/runs/{run_id}/monitor",
    "job submit": "/api/v2/jobs",
    "job unfinished": "/api/v2/jobs/unfinished",
    "model doctor": "/api/v2/models/{model_id:path}/doctor",
    "model install": "/api/v2/models/{model_id:path}/install",
    "model list": "/api/v2/models",
    "model proxy": "/api/v2/models/proxies",
    "model remote add": "/api/v2/models/remotes",
    "model remote list": "/api/v2/models/remotes",
    "model remote remove": "/api/v2/models/remotes/{name}",
    "model remove": "/api/v2/models/{model_id:path}/remove",
    "model show": "/api/v2/models/{model_id:path}",
    "model update": "/api/v2/models/{model_id:path}/update",
    "node add": "/api/v2/system/cluster/nodes:add",
    "node drain": "/api/v2/nodes/{node_name}/drain",
    "node join": "/api/v2/system/cluster/nodes:join",
    "node leave": "/api/v2/system/cluster/nodes:leave",
    "node list": "/api/v2/nodes",
    "node maintenance": "/api/v2/nodes/{node_name}/maintenance",
    "node reconcile": "/api/v2/nodes/{node_name}/reconcile",
    "node undrain": "/api/v2/nodes/{node_name}/undrain",
    "resource list": "/api/v2/resource",
    "resource ports": "/api/v2/resource/ports",
    "resource set": "/api/v2/resource",
    "runtime doctor": "/api/v2/runtime/doctor",
    "runtime health": "/api/v2/runtime/health",
    "runtime metrics": "/api/v2/metrics",
    "runtime status": "/api/v2/runtime/status",
    "schedule create": "/api/v2/schedules",
    "schedule delay": "/api/v2/schedules/delayed",
    "schedule delete": "/api/v2/schedules/{schedule_id}",
    "schedule list": "/api/v2/schedules",
    "schedule pause": "/api/v2/schedules/{schedule_id}/pause",
    "schedule resume": "/api/v2/schedules/{schedule_id}/resume",
    "schedule run-now": "/api/v2/schedules/{schedule_id}/dispatch",
    "schedule status": "/api/v2/schedules/{schedule_id}",
    "service check": "/api/v2/services:check",
    "service list": "/api/v2/services",
    "service resolve": "/api/v2/services/{name}/resolve",
    "trigger create": "/api/v2/triggers",
    "trigger delete": "/api/v2/triggers/{schedule_id}",
    "trigger list": "/api/v2/triggers",
}
