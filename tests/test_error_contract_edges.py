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


def test_resource_overloaded_legacy_response_is_preserved():
    response = handle_grpc_error(
        RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, "resource_overloaded: memory=0.99 threshold=0.95")
    )

    assert response.status_code == 503
    assert json.loads(response.body)["error"] == "resource_overloaded"


def test_failed_precondition_validation_problem_parses_json_report():
    response = handle_grpc_error(
        RpcError(
            grpc.StatusCode.FAILED_PRECONDITION,
            'requirements_not_met: {"errors": ["gpu missing"], "issues": [{"code": "gpu", "message": "missing"}]}',
        )
    )

    body = json.loads(response.body)
    assert response.status_code == 412
    assert body["error"] == "requirements_not_met"
    assert body["detail"] == "gpu missing"
    assert body["validation"]["errors"] == ["gpu missing"]


def test_invalid_argument_validation_problem_builds_legacy_report_for_plain_detail():
    response = handle_grpc_error(
        RpcError(grpc.StatusCode.INVALID_ARGUMENT, "input_validation_failed: missing input document")
    )

    body = json.loads(response.body)
    assert response.status_code == 422
    assert body["error"] == "input_validation_failed"
    assert body["validation"]["error_count"] == 1
