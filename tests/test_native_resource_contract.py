"""REST contract routing, request fencing, uploads, and native stream errors."""

import json
import re
import pytest
from fastapi.testclient import TestClient
from mn_api.app import create_app
from mn_sdk.runtime_contract.endpoints import ENDPOINTS
from mn_sdk.proto import runtime_control_pb2 as wire
from tests.test_client_app import native as native_fixture, connected, reply

native = native_fixture

READS = [item for item in ENDPOINTS if item["method"] == "GET" and item["action"] != "get_health"]


@pytest.mark.parametrize("item", READS, ids=lambda item: item["action"])
def test_public_reads_use_one_native_resource_and_preserve_status(native, item):
    connected()
    # A native rejection is an authoritative resource response, never a signal
    # to execute locally. Verify every declared public read reaches its action.
    native.config.response = reply(
        {"code": "resource_changed", "detail": "Refresh this resource."}, 409, **{"Retry-After": "2"}
    )
    native.config.frames = [
        wire.RuntimeFrame(version=1, kind="headers", reply=native.config.response),
        wire.RuntimeFrame(version=1, kind="data", data=b'{"code":"resource_changed"}'),
    ]
    path = re.sub(r"\{[^}]+\}", "example", item["path"].replace("{output_index}", "0"))
    if item["action"] == "stream_signals":
        path += "?channel=errors"
    with TestClient(create_app(), headers={"Authorization": "Bearer local-secret"}) as api:
        result = api.get("/api/v1" + path)
    assert result.status_code == 409, result.text
    assert result.headers["Retry-After"] == "2"
    assert len(native.calls) == 1 and native.calls[0][1] == item["action"]
    assert native.calls[0][4].token == "remote-secret"


@pytest.mark.parametrize(
    "method,path,body,action",
    [
        ("POST", "/jobs", {"blueprint_id": "demo"}, "create_job"),
        ("PATCH", "/runs/run", {"desired_state": "cancelled"}, "update_run"),
        ("POST", "/jobs/job/runs", {}, "create_job_run"),
        ("POST", "/jobs/job/run-operations", {}, "create_job_run_operation"),
        ("POST", "/jobs/job/preparations", {}, "prepare_job_update"),
        ("POST", "/jobs/job/relocations", {"target_node": "worker@test", "transfer_id": "a" * 32}, "relocate_job"),
        ("POST", "/jobs/job/data-resets", {}, "create_job_data_reset"),
        ("POST", "/jobs/job/run-admission-checks", {}, "check_job_run_admission"),
        (
            "POST",
            "/jobs/job/schedules",
            {"schedule": {"kind": "delayed", "run_at": "2099-01-01T00:00:00Z"}},
            "create_job_schedule",
        ),
        ("POST", "/runs/run/retry-plans", {}, "plan_run_retry"),
        ("POST", "/runs/run/retries", {"expected_attempt": 1, "checkpoint_revision": "a" * 64}, "retry_run"),
        ("POST", "/blueprints/demo/validations", {}, "create_blueprint_validation"),
        ("POST", "/blueprints/demo/additions", {}, "create_blueprint_addition"),
        ("POST", "/blueprints/demo/removals", {}, "create_blueprint_removal"),
        ("POST", "/blueprint-catalog-refreshes", {}, "create_blueprint_catalog_refresh"),
        ("POST", "/blueprint-cleanups", {}, "create_blueprint_cleanup"),
        ("PUT", "/nodes/worker/drain", {}, "create_node_drain"),
        ("PATCH", "/nodes/worker", {"maintenance": True}, "update_node"),
        ("POST", "/nodes/worker/reconciliations", {}, "create_node_reconciliation"),
    ],
)
def test_mutations_preserve_contract_and_private_credential(native, method, path, body, action):
    connected()
    native.config.response = reply({"accepted": True}, 202, Location="/api/v1/operations/op")
    with TestClient(
        create_app(),
        headers={"Authorization": "Bearer local-secret", "Idempotency-Key": "request-id", "If-Match": '"revision"'},
    ) as api:
        response = api.request(method, "/api/v1" + path, json=body)
    assert response.status_code == 202, response.text
    assert response.headers["Location"] == "/api/v1/operations/op"
    assert native.calls[0][1] == action
    assert "local-secret" not in json.dumps(native.calls[0][2])


@pytest.mark.parametrize("multipart", [True, False])
def test_existing_uploads_are_streamed_and_preserve_receipts(native, multipart):
    connected()
    native.config.response = reply({"operation_id": "op"}, 202, Location="/api/v1/operations/op")
    payload = b"zip-content" * 20000
    with TestClient(create_app(), headers={"Authorization": "Bearer local-secret"}) as api:
        if multipart:
            response = api.post("/api/v1/bundles", files={"bundle": ("blueprint.zip", payload, "application/zip")})
        else:
            response = api.post(
                "/api/v1/job-restorations?job_id=restored", content=payload, headers={"Content-Type": "application/zip"}
            )
    assert response.status_code == 202, response.text
    assert native.calls[0][0] == "Upload" and native.calls[0][3] == payload
    assert response.headers["Location"] == "/api/v1/operations/op"


def test_wrong_upload_type_and_invalid_mutation_never_reach_runtime(native):
    connected()
    with TestClient(create_app(), headers={"Authorization": "Bearer local-secret"}) as api:
        assert api.post("/api/v1/job-restorations", content=b"bad").status_code == 415
        assert api.patch("/api/v1/runs/run", json={"desired_state": "unknown"}).status_code == 422
    assert not native.calls


def test_empty_native_response_remains_empty(native):
    connected()
    native.config.response = wire.RuntimeReply(version=1, status=204)
    with TestClient(create_app(), headers={"Authorization": "Bearer local-secret"}) as api:
        response = api.delete("/api/v1/runs/run")
    assert response.status_code == 204 and not response.content
