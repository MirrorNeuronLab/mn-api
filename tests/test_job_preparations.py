from __future__ import annotations

import json
from threading import Event
import time
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from mn_api import operations, state
from mn_api.app import create_app
from mn_api.http_semantics import strong_etag
from mn_api.routes.v1 import jobs


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "resolve_mn_home", lambda: tmp_path)
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="", request_size_limit_bytes=1024*1024,
                                                       cors_allow_origins=[]))
    records = {}
    def read(job_id):
        if job_id not in records:
            raise HTTPException(404, "Missing")
        return records[job_id]
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(get_job=read))
    return TestClient(create_app()), records


def wait(client, operation_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        operation = client.get(f"/api/v1/operations/{operation_id}").json()
        if operation["status"] in {"completed", "failed"}:
            return operation
        time.sleep(.01)
    raise AssertionError("Work did not finish")


def restart():
    with operations._condition:
        operations._operations.clear()
        operations._local_operation_ids.clear()
        operations._local_operation_events.clear()
        operations._durable_local_operation_ids.clear()


def test_creation_claim_precedes_work_and_reconnect_never_creates_another_job(api, monkeypatch):
    client, records = api
    started, release = Event(), Event()
    calls = []
    def create(request, key, principal, progress_id):
        calls.append(request.job_id)
        started.set()
        assert release.wait(2)
        result = {"job_id": request.job_id, "blueprint_id": request.blueprint_id,
                  "resolved_configuration": request.resolved_configuration}
        records[request.job_id] = result
        return JSONResponse(result)
    monkeypatch.setattr(jobs, "create_job", create)
    headers = {"Idempotency-Key": "one-hire"}
    body = {"blueprint_id": "research", "resolved_configuration": {"mode": "sample"}}
    accepted = client.post("/api/v1/job-preparations", headers=headers, json=body)
    assert accepted.status_code == 202
    operation_id = accepted.json()["operation_id"]
    assert started.wait(1)
    assert client.post("/api/v1/job-preparations", headers=headers, json=body).json()["operation_id"] == operation_id
    changed = client.post("/api/v1/job-preparations", headers=headers,
                          json={**body, "resolved_configuration": {"mode": "own"}})
    assert changed.status_code == 409
    assert changed.headers["location"].endswith(operation_id)
    release.set()
    result = wait(client, operation_id)
    assert result["status"] == "completed"
    assert len(calls) == 1
    # Simulate losing the completion receipt across an API restart after Core
    # committed. The fixed identity is reconciled before any preparation runs.
    path = operations._durable_operation_path(operation_id)
    saved = json.loads(path.read_text())
    saved["operation"]["status"] = "running"
    path.write_text(json.dumps(saved))
    restart()
    assert client.get(f"/api/v1/operations/{operation_id}").json()["status"] == "interrupted"
    client.post("/api/v1/job-preparations", headers=headers, json=body)
    assert wait(client, operation_id)["result"]["job_id"] == calls[0]
    assert len(calls) == 1


def test_conditional_update_is_short_and_reconciles_a_committed_revision(api, monkeypatch):
    client, records = api
    records["stable"] = {"job_id": "stable", "revision": 1, "resolved_configuration": {"mode": "old"}}
    headers = {"Idempotency-Key": "settings-revision", "If-Match": strong_etag(records["stable"])}
    started, release = Event(), Event()
    calls = []
    def update(job_id, request, etag, principal):
        calls.append(etag)
        started.set()
        assert release.wait(2)
        records[job_id] = {"job_id": job_id, "revision": 2, "resolved_configuration": request.resolved_configuration}
        return JSONResponse(records[job_id])
    monkeypatch.setattr(jobs, "update_job", update)
    body = {"resolved_configuration": {"mode": "new"}}
    accepted = client.post("/api/v1/jobs/stable/preparations", headers=headers, json=body)
    assert accepted.status_code == 202
    assert started.wait(1)
    operation_id = accepted.json()["operation_id"]
    release.set()
    assert wait(client, operation_id)["result"]["revision"] == 2
    path = operations._durable_operation_path(operation_id)
    saved = json.loads(path.read_text())
    saved["operation"]["status"] = "running"
    path.write_text(json.dumps(saved))
    restart()
    client.post("/api/v1/jobs/stable/preparations", headers=headers, json=body)
    assert wait(client, operation_id)["result"]["revision"] == 2
    assert len(calls) == 1


def test_async_preparation_validates_claim_and_conditional_headers_before_work(api):
    client, _ = api
    assert client.post("/api/v1/job-preparations", json={"blueprint_id": "research"}).status_code == 400
    assert client.post("/api/v1/jobs/stable/preparations", headers={"Idempotency-Key": "update"},
                       json={"resolved_configuration": {}}).status_code == 428
    assert client.post("/api/v1/jobs/stable/preparations", headers={"Idempotency-Key": "update", "If-Match": '"one"'},
                       json={"status": "archived"}).status_code == 422


def test_corrected_failed_preparation_keeps_the_reserved_job_identity(api, monkeypatch):
    client, records = api
    calls = []
    def create(request, *_):
        calls.append(request.job_id)
        if request.resolved_configuration.get("mode") == "invalid":
            raise HTTPException(422, "Invalid setup")
        records[request.job_id] = {"job_id": request.job_id, "blueprint_id": request.blueprint_id,
                                   "resolved_configuration": request.resolved_configuration}
        return JSONResponse(records[request.job_id])
    monkeypatch.setattr(jobs, "create_job", create)
    headers = {"Idempotency-Key": "one-hire"}
    first = client.post("/api/v1/job-preparations", headers=headers,
                        json={"blueprint_id": "research", "resolved_configuration": {"mode": "invalid"}}).json()
    assert wait(client, first["operation_id"])["status"] == "failed"
    second = client.post("/api/v1/job-preparations", headers=headers,
                         json={"blueprint_id": "research", "resolved_configuration": {"mode": "sample"}}).json()
    assert second["operation_id"] != first["operation_id"]
    assert wait(client, second["operation_id"])["status"] == "completed"
    assert len(set(calls)) == 1


def test_first_update_keeps_the_original_conditional_write_fence(api, monkeypatch):
    client, records = api
    records["stable"] = {"job_id": "stable", "revision": 2, "resolved_configuration": {"mode": "new"}}
    calls = []
    def update(job_id, request, etag, principal):
        calls.append(etag)
        raise HTTPException(412, "Revision changed")
    monkeypatch.setattr(jobs, "update_job", update)
    accepted = client.post("/api/v1/jobs/stable/preparations",
        headers={"Idempotency-Key": "stale-write", "If-Match": '"old"'},
        json={"resolved_configuration": {"mode": "new"}})
    assert wait(client, accepted.json()["operation_id"])["status"] == "failed"
    assert calls == ['"old"']
    repeated = client.post("/api/v1/jobs/stable/preparations",
        headers={"Idempotency-Key": "stale-write", "If-Match": '"old"'},
        json={"resolved_configuration": {"mode": "new"}})
    assert wait(client, repeated.json()["operation_id"])["status"] == "failed"
    assert calls == ['"old"', '"old"']


def test_explicit_retry_of_failed_preparation_reconciles_the_same_job(api, monkeypatch):
    client, records = api
    calls = []
    def create(request, *_):
        calls.append(request.job_id)
        if len(calls) == 1:
            raise HTTPException(409, "Resources are not ready")
        records[request.job_id] = {"job_id": request.job_id, "blueprint_id": request.blueprint_id,
                                   "resolved_configuration": request.resolved_configuration}
        return JSONResponse(records[request.job_id])
    monkeypatch.setattr(jobs, "create_job", create)
    headers, body = {"Idempotency-Key": "same-hire"}, {"blueprint_id": "research"}
    first = client.post("/api/v1/job-preparations", headers=headers, json=body).json()
    assert wait(client, first["operation_id"])["status"] == "failed"
    # Merely reading a failure never redispatches work.
    assert wait(client, first["operation_id"])["status"] == "failed"
    assert len(calls) == 1
    second = client.post("/api/v1/job-preparations", headers=headers, json=body).json()
    assert second["operation_id"] == first["operation_id"]
    assert wait(client, second["operation_id"])["status"] == "completed"
    assert len(set(calls)) == 1
