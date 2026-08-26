from __future__ import annotations

import json

import grpc

from mn_api.errors import handle_grpc_error


class RpcError(grpc.RpcError):
    def __init__(self, code, detail):
        self._code = code
        self._detail = detail

    def code(self):
        return self._code

    def details(self):
        return self._detail


def test_resource_overloaded_uses_current_app_error_contract():
    response = handle_grpc_error(
        RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "resource_overloaded: memory=0.99 threshold=0.95")
    )

    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "MN_RESOURCE_EXHAUSTED"


def test_failed_precondition_does_not_parse_detail_encoded_validation_reports():
    response = handle_grpc_error(
        RpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            'requirements_not_met: {"errors": ["gpu missing"], "issues": [{"code": "gpu", "message": "missing"}]}',
        )
    )

    body = json.loads(response.body)
    assert response.status_code == 412
    assert body["code"] == "MN_FAILED_PRECONDITION"
    assert "errors" not in body


def test_coordination_store_mismatch_preserves_machine_readable_error():
    response = handle_grpc_error(
        RpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            "placement_failed: coordination_store_mismatch: node-a uses a read-only replica",
        )
    )

    body = json.loads(response.body)
    assert response.status_code == 412
    assert body["code"] == "MN_COORDINATION_STORE_MISMATCH"
    assert "legacy" not in body["detail"].lower()


def test_service_run_exists_is_a_stable_conflict():
    response = handle_grpc_error(
        RpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            "MN_SERVICE_RUN_EXISTS: service job job-1 already has run run-1",
        )
    )

    body = json.loads(response.body)
    assert response.status_code == 409
    assert body["code"] == "MN_SERVICE_RUN_EXISTS"
    assert body["detail"] == "This service job already has a run."


def test_invalid_argument_does_not_build_validation_report_from_plain_detail():
    response = handle_grpc_error(
        RpcError(grpc.StatusCode.INVALID_ARGUMENT, "input_validation_failed: missing input document")
    )

    body = json.loads(response.body)
    assert response.status_code == 422
    assert body["code"] == "MN_INVALID_ARGUMENT"
    assert "errors" not in body
