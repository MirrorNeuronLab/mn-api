import grpc
from unittest.mock import patch

from fastapi.testclient import TestClient

from mn_api.app import create_app
from mn_api.errors import handle_grpc_error


class PermissionRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.PERMISSION_DENIED

    def details(self):
        return "ClearJobs requires MN_GRPC_ADMIN_TOKEN secret-token"


def test_unexpected_api_error_returns_safe_problem_response():
    app = create_app()

    @app.get("/boom")
    def boom():
        raise RuntimeError("backend unavailable at /Users/homer/private.py token=secret-token")

    client = TestClient(app, raise_server_exceptions=False)

    with patch("mn_api.state.logger.error") as log:
        response = client.get("/boom", headers={"x-request-id": "req-123"})

    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == "MN_EXECUTION_FAILED"
    assert payload["detail"] == "Execution failed. Run again with --debug for more details."
    assert payload["request_id"] == "req-123"
    rendered = repr(payload)
    assert "backend unavailable" not in rendered
    assert "secret-token" not in rendered
    assert "/Users/homer" not in rendered
    log.assert_called_once()
    assert log.call_args.args[1] == "MN_EXECUTION_FAILED"
    assert log.call_args.kwargs["exc_info"]


def test_grpc_permission_error_uses_stable_code_without_detail_leak():
    with patch("mn_api.state.logger.error") as log:
        response = handle_grpc_error(PermissionRpcError())
    payload = response.body.decode("utf-8")

    assert response.status_code == 403
    assert "MN_PERMISSION_DENIED" in payload
    assert "Permission was denied" in payload
    assert "MN_GRPC_ADMIN_TOKEN" not in payload
    assert "secret-token" not in payload
    log.assert_called_once()


def test_placement_failure_returns_shared_hardware_problem():
    from mn_sdk.admission_errors import placement_error

    app = create_app()

    @app.get("/placement-test")
    def placement_test():
        raise placement_error(
            {"secret-node": ["host_memory_total_mb=24576 < required=49152"]},
            "token=secret /Users/private/file",
        )

    with patch("mn_api.state.logger.error"):
        response = TestClient(app, raise_server_exceptions=False).get("/placement-test")
    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert body["code"] == "MN_MEMORY_REQUIREMENT_UNMET"
    assert body["problem_code"] == 1001
    assert body["category"] == "hardware"
    assert body["retryable"] is False
    assert "requires 48 GiB; available 24 GiB" in body["detail"]
    assert body["details"]["blockers"][0]["available"] == 24
    assert "secret" not in response.text
    assert "/Users" not in response.text
