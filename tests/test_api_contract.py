from __future__ import annotations

from types import SimpleNamespace
import warnings

from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.routes import blueprints, bundles, deployments, jobs, models, runs, schedules, services, system
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


def test_representative_protected_routes_require_bearer_token(monkeypatch, api_client, state_snapshot):
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(api_token="secret", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]),
    )

    for path in (
        "/api/v1/runtime/status",
        "/api/v1/jobs",
        "/api/v1/models",
        "/api/v1/services",
        "/api/v1/resource",
        "/api/v1/runs",
        "/api/v1/blueprints",
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

    assert schema["paths"]["/api/v1/jobs:cleanup"]["post"]["operationId"] == "cleanup_jobs_colon_alias"
    assert schema["paths"]["/api/v1/jobs/cleanup"]["post"]["operationId"] == "cleanup_jobs_path_alias"
    assert schema["paths"]["/api/v1/services:check"]["post"]["operationId"] == "check_services_colon_alias"
    assert schema["paths"]["/api/v1/services/check"]["post"]["operationId"] == "check_services_path_alias"


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

    response = TestClient(create_app()).get("/api/v1/health", headers={"authorization": "Bearer secret"})

    assert response.status_code == 200
    assert response.json()["auth"] == "enabled"


def _api_route_paths() -> set[str]:
    modules = (blueprints, bundles, deployments, jobs, models, runs, schedules, services, system)
    return {route.path for module in modules for route in module.router.routes}


COMMAND_ENDPOINTS = {
    "blueprint cleanup": "/api/v1/blueprints:cleanup",
    "blueprint compare": "/api/v1/runs:compare",
    "blueprint doctor": "/api/v1/blueprints/{blueprint_id}/validate",
    "blueprint export": "/api/v1/runs/{run_id}/export",
    "blueprint human": "/api/v1/runs/{run_id}/human",
    "blueprint human ack": "/api/v1/runs/{run_id}/human/{notice_id}/ack",
    "blueprint human respond": "/api/v1/runs/{run_id}/human/{request_id}/response",
    "blueprint install": "/api/v1/blueprints/{blueprint_id}/install",
    "blueprint list": "/api/v1/blueprints",
    "blueprint logs": "/api/v1/runs/{run_id}/logs",
    "blueprint monitor": "/api/v1/runs/ws",
    "blueprint resources": "/api/v1/runs/{run_id}/resources",
    "blueprint run": "/api/v1/blueprints/{blueprint_id}/runs",
    "blueprint stream": "/api/v1/runs/{run_id}/stream",
    "blueprint tail": "/api/v1/runs/{run_id}/events",
    "blueprint uninstall": "/api/v1/blueprints:uninstall",
    "blueprint validate": "/api/v1/blueprints/{blueprint_id}/validate",
    "deployment deploy": "/api/v1/deployments",
    "deployment fail": "/api/v1/deployments/{id_or_key}/fail",
    "deployment list": "/api/v1/deployments",
    "deployment pause": "/api/v1/deployments/{id_or_key}/pause",
    "deployment promote": "/api/v1/deployments/{id_or_key}/promote",
    "deployment resume": "/api/v1/deployments/{id_or_key}/resume",
    "deployment rollback": "/api/v1/deployments/{id_or_key}/rollback",
    "deployment status": "/api/v1/deployments/{id_or_key}",
    "event emit": "/api/v1/events",
    "event list": "/api/v1/events",
    "job backup": "/api/v1/jobs/{job_id}/backup",
    "job cancel": "/api/v1/jobs/{job_id}/cancel",
    "job clear": "/api/v1/jobs:cleanup",
    "job dead-letters": "/api/v1/jobs/{job_id}/dead-letters",
    "job list": "/api/v1/jobs",
    "job monitor": "/api/v1/jobs/{job_id}/workflow-progress/stream",
    "job pause": "/api/v1/jobs/{job_id}/pause",
    "job restore": "/api/v1/jobs/restore",
    "job result": "/api/v1/jobs/{job_id}",
    "job resume": "/api/v1/jobs/{job_id}/resume",
    "job status": "/api/v1/jobs/{job_id}",
    "job submit": "/api/v1/jobs",
    "job unfinished": "/api/v1/jobs/unfinished",
    "model doctor": "/api/v1/models/{model_id:path}/doctor",
    "model install": "/api/v1/models/{model_id:path}/install",
    "model list": "/api/v1/models",
    "model proxy": "/api/v1/models/proxies",
    "model remote add": "/api/v1/models/remotes",
    "model remote list": "/api/v1/models/remotes",
    "model remote remove": "/api/v1/models/remotes/{name}",
    "model remove": "/api/v1/models/{model_id:path}/remove",
    "model show": "/api/v1/models/{model_id:path}",
    "model update": "/api/v1/models/{model_id:path}/update",
    "node add": "/api/v1/system/cluster/nodes:add",
    "node drain": "/api/v1/nodes/{node_name}/drain",
    "node join": "/api/v1/system/cluster/nodes:join",
    "node leave": "/api/v1/system/cluster/nodes:leave",
    "node list": "/api/v1/nodes",
    "node maintenance": "/api/v1/nodes/{node_name}/maintenance",
    "node reconcile": "/api/v1/nodes/{node_name}/reconcile",
    "node undrain": "/api/v1/nodes/{node_name}/undrain",
    "resource list": "/api/v1/resource",
    "resource ports": "/api/v1/resource/ports",
    "resource set": "/api/v1/resource",
    "runtime doctor": "/api/v1/runtime/doctor",
    "runtime health": "/api/v1/runtime/health",
    "runtime metrics": "/api/v1/metrics",
    "runtime status": "/api/v1/runtime/status",
    "schedule create": "/api/v1/schedules",
    "schedule delay": "/api/v1/schedules/delayed",
    "schedule delete": "/api/v1/schedules/{schedule_id}",
    "schedule list": "/api/v1/schedules",
    "schedule pause": "/api/v1/schedules/{schedule_id}/pause",
    "schedule resume": "/api/v1/schedules/{schedule_id}/resume",
    "schedule run-now": "/api/v1/schedules/{schedule_id}/dispatch",
    "schedule status": "/api/v1/schedules/{schedule_id}",
    "service check": "/api/v1/services:check",
    "service list": "/api/v1/services",
    "service resolve": "/api/v1/services/{name}/resolve",
    "trigger create": "/api/v1/triggers",
    "trigger delete": "/api/v1/triggers/{schedule_id}",
    "trigger list": "/api/v1/triggers",
}
