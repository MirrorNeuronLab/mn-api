"""Keep the REST surface consumed by OtterDesk visible in the API test gate.

The inventory comes from otterdesk-desktop-app/src/main/services/worker-blueprints/
service.js and mirror-neuron-runtime/service.js. Behavioral tests for these
routes live beside their owning route and service tests.
"""

import pytest

from mn_api.app import create_app


OTTERDESK_ROUTES = (
    ("GET", "/health", "get_health"),
    ("GET", "/runtime/health", "get_runtime_health"),
    ("GET", "/runtime/diagnostics", "get_runtime_diagnostics"),
    ("GET", "/runtime/resources", "get_runtime_resources"),
    ("PUT", "/runtime/resources", "replace_runtime_resources"),
    ("GET", "/system/summary", "get_system_summary"),
    ("GET", "/metrics", "get_metrics"),
    ("GET", "/nodes", "list_nodes"),
    ("POST", "/nodes", "create_node"),
    ("DELETE", "/nodes/{node_id}", "delete_node"),
    ("GET", "/blueprints", "list_blueprints"),
    ("GET", "/blueprints/{blueprint_id}", "get_blueprint"),
    ("POST", "/blueprints/{blueprint_id}/additions", "create_blueprint_addition"),
    ("POST", "/blueprints/{blueprint_id}/validations", "create_blueprint_validation"),
    ("POST", "/blueprints/{blueprint_id}/runs", "create_blueprint_run"),
    ("POST", "/jobs", "create_job"),
    ("GET", "/jobs", "list_jobs"),
    ("GET", "/jobs/{job_id}", "get_job"),
    ("PATCH", "/jobs/{job_id}", "update_job"),
    ("DELETE", "/jobs/{job_id}", "delete_job"),
    ("GET", "/jobs/{job_id}/ui", "get_job_ui"),
    ("GET", "/jobs/{job_id}/workflow/definition/dag", "get_job_definition_dag"),
    ("GET", "/jobs/{job_id}/workflow/definition/steps", "get_job_definition_steps"),
    ("GET", "/jobs/{job_id}/workflow/latest-run/dag", "get_job_latest_run_dag"),
    ("GET", "/jobs/{job_id}/workflow/latest-run/steps", "get_job_latest_run_steps"),
    ("POST", "/jobs/{job_id}/runs", "create_job_run"),
    ("POST", "/jobs/{job_id}/schedules", "create_job_schedule"),
    ("GET", "/runs/{run_id}", "get_run"),
    ("PATCH", "/runs/{run_id}", "update_run"),
    ("GET", "/runs/{run_id}/monitor", "get_run_monitor"),
    ("GET", "/runs/{run_id}/workflow-progress", "get_run_workflow_progress"),
    ("GET", "/runs/{run_id}/events/stream", "stream_run_events"),
    ("GET", "/runs/{run_id}/events", "list_run_events"),
    ("GET", "/runs/{run_id}/logs", "list_run_logs"),
    ("GET", "/runs/{run_id}/resources", "get_run_resources"),
    ("GET", "/runs/{run_id}/human-requests", "list_run_human_requests"),
    ("POST", "/runs/{run_id}/human-requests/{request_id}/responses", "create_run_human_response"),
    ("POST", "/runs/{run_id}/human-requests/{request_id}/acknowledgements", "create_run_human_acknowledgement"),
    ("GET", "/runs/{run_id}/artifacts/final", "get_run_final_artifact"),
    ("GET", "/runs/{run_id}/artifacts", "list_run_artifacts"),
    ("GET", "/runs/{run_id}/outputs", "list_run_outputs"),
    ("GET", "/runs/{run_id}/observability", "get_run_observability"),
    ("GET", "/runs/{run_id}/snapshots", "get_run_snapshot"),
    ("GET", "/operations/{operation_id}", "get_operation"),
    ("GET", "/services", "list_services"),
)


@pytest.fixture(scope="module")
def api_app():
    return create_app()


@pytest.mark.parametrize("method,path,operation_id", OTTERDESK_ROUTES)
def test_otterdesk_rest_route_is_registered(api_app, method, path, operation_id):
    operation = api_app.openapi()["paths"][f"/api/v1{path}"][method.lower()]
    assert operation["operationId"] == operation_id


def test_otterdesk_job_mcp_mount_is_registered(api_app):
    assert any(getattr(route, "path", None) == "/api/v1/jobs/{job_id}" for route in api_app.routes)


@pytest.mark.xfail(strict=True, reason="Schedule read/update RPCs are not yet available in Core and the SDK")
@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_otterdesk_schedule_detail_route_is_registered(api_app, method):
    assert method.lower() in api_app.openapi()["paths"]["/api/v1/schedules/{schedule_id}"]
