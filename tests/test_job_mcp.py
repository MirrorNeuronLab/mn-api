from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.job_mcp import (
    MAX_CONTEXT_BYTES,
    JOB_CONTEXT_SCHEMA,
    JobContextProvider,
    context_state,
    safe_context_value,
)
from mn_api import job_mcp


class MCPRuntime:
    def __init__(self) -> None:
        self.jobs = {
            "job-1": {
                "job_id": "job-1",
                "blueprint_id": "chat-blueprint",
                "status": "active",
                "resolved_configuration": {
                    "query": "safe",
                    "api_token": "must-not-leak",
                    "nested": {"password": "must-not-leak", "region": "east"},
                    "output_path": "/private/tmp/hidden",
                },
            },
            "job-2": {"job_id": "job-2", "blueprint_id": "chat-blueprint", "status": "archived"},
            "job-deleted": {
                "job_id": "job-deleted",
                "blueprint_id": "chat-blueprint",
                "status": "active",
                "deleted": True,
            },
            "job-disabled": {"job_id": "job-disabled", "blueprint_id": "disabled-blueprint", "status": "active"},
        }
        self.runs: dict[str, list[dict]] = {"job-1": [], "job-2": []}

    def get_job(self, job_id):
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="missing")
        return json.dumps(self.jobs[job_id])

    def list_runs(self, job_id, *, page_size=50, page_token=""):
        del page_size, page_token
        return json.dumps({"items": self.runs.get(job_id, [])})

    def get_run(self, run_id):
        for run_items in self.runs.values():
            for run in run_items:
                if run.get("run_id") == run_id:
                    return json.dumps(run)
        raise HTTPException(status_code=404, detail="missing")

def _blueprint(_config, blueprint_id):
    enabled = blueprint_id != "disabled-blueprint"
    return "catalog", {
        "id": blueprint_id,
        "name": "Queue Reviewer",
        "description": "Review the account queue and explain the latest result.",
        "capabilities": ["Review", "Explain"],
        "mcp_collaboration": {
            "enabled": enabled,
            "service_name": "mn-job-collaboration",
            "transport": "streamable-http",
            "path": "/mcp",
            "goal_id": "goal-1",
        },
    }


def _configure(monkeypatch, runtime: MCPRuntime, *, token: str = "") -> None:
    monkeypatch.setattr(state, "client", runtime)
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(api_token=token, request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]),
    )
    monkeypatch.setattr(job_mcp, "find_blueprint", _blueprint)
    monkeypatch.setattr(job_mcp.runtime_job_routes, "_workflow_progress_snapshot_for_job", lambda _run_id: {"steps": []})
    monkeypatch.setattr(
        job_mcp.runtime_run_routes,
        "get_run_final_artifact",
        lambda _run_id, _principal: (_ for _ in ()).throw(HTTPException(status_code=404, detail="missing")),
    )


def _mcp_request(client: TestClient, job_id: str, body: dict, *, token: str = ""):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(f"/api/v1/jobs/{job_id}/mcp", headers=headers, json=body)


def _initialize(client: TestClient, job_id: str = "job-1", *, token: str = ""):
    return _mcp_request(
        client,
        job_id,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mn-api-test", "version": "1"},
            },
        },
        token=token,
    )


def test_protocol_lists_only_stable_read_tools_and_reads_never_run_context(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)

    with TestClient(create_app()) as client:
        assert _initialize(client).status_code == 200
        listed = _mcp_request(
            client,
            "job-1",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "get_job_context",
            "get_job_profile",
            "get_latest_run",
        }
        called = _mcp_request(
            client,
            "job-1",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_job_context", "arguments": {"evidence_limit": 50}},
            },
        ).json()

    context = called["result"]["structuredContent"]
    assert context["schema_version"] == JOB_CONTEXT_SCHEMA
    assert context["identity"] == {"job_id": "job-1", "blueprint_id": "chat-blueprint", "goal_id": "goal-1"}
    assert context["state"] == "never_run"
    assert context["latest_run"] is None
    encoded = json.dumps(context)
    assert "must-not-leak" not in encoded
    assert "/private/tmp/hidden" not in encoded
    assert context["profile"]["configuration"] == {"query": "safe", "nested": {"region": "east"}}


def test_auth_job_isolation_archived_and_disabled_behavior(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime, token="top-secret")

    with TestClient(create_app()) as client:
        unauthenticated = _initialize(client)
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "unauthorized"
        assert _initialize(client, token="wrong").status_code == 401
        assert _initialize(client, token="top-secret").status_code == 200

        archived = _mcp_request(
            client,
            "job-2",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_job_profile", "arguments": {}},
            },
            token="top-secret",
        ).json()["result"]["structuredContent"]
        assert archived["identity"]["job_id"] == "job-2"
        assert archived["state"] == "archived"

        disabled = _initialize(client, "job-disabled", token="top-secret")
        assert disabled.status_code == 404
        assert disabled.json()["code"] == "job_mcp_not_found"
        missing = _initialize(client, "missing", token="top-secret")
        assert missing.status_code == 404
        deleted = _initialize(client, "job-deleted", token="top-secret")
        assert deleted.status_code == 404
        assert deleted.json()["detail"] == disabled.json()["detail"] == missing.json()["detail"]


def test_context_states_latest_result_partial_warning_and_bounds(monkeypatch):
    runtime = MCPRuntime()
    runtime.jobs["job-1"]["latest_run_id"] = "run-1"
    runtime.runs["job-1"] = [
        {
            "job_id": "job-1",
            "run_id": "run-1",
            "runtime_run_id": "runtime-1",
            "status": "completed",
            "started_at": "2026-08-13T12:00:00Z",
            "completed_at": "2026-08-13T12:05:00Z",
        }
    ]
    runtime.jobs["job-1"]["schedules"] = [
        {"schedule_id": "schedule-1", "job_id": "job-1", "status": "running"}
    ]
    runtime.jobs["job-1"]["resolved_configuration"] = {f"field_{index}": "x" * 10_000 for index in range(200)}
    _configure(monkeypatch, runtime)
    monkeypatch.setattr(
        job_mcp.runtime_job_routes,
        "_workflow_progress_snapshot_for_job",
        lambda _run_id: {
            "status": "completed",
            "steps": [
                {"id": f"step-{index}", "name": f"Step {index}", "status": "completed"}
                for index in range(80)
            ],
        },
    )
    monkeypatch.setattr(
        job_mcp.runtime_run_routes,
        "get_run_final_artifact",
        lambda _run_id, _principal: {"summary": "Finished safely", "access_token": "must-not-leak"},
    )

    context = JobContextProvider().get_context("job-1", evidence_limit=50)
    assert context["state"] == "scheduled_waiting"
    assert context["latest_run"]["result"] == {"summary": "Finished safely"}
    assert len(context["evidence"]) <= 50
    assert len(json.dumps(context).encode()) <= MAX_CONTEXT_BYTES
    assert context["truncation"]["truncated"] is True

    monkeypatch.setattr(
        job_mcp.runtime_job_routes,
        "_workflow_progress_snapshot_for_job",
        lambda _run_id: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    partial = JobContextProvider().get_context("job-1")
    assert partial["profile"]["identity"]["job_id"] == "job-1"
    assert any("Workflow evidence" in warning for warning in partial["warnings"])


def test_safe_context_value_removes_secret_and_path_keys_recursively():
    value = safe_context_value(
        {
            "safe": {"region": "east", "client_secret": "hidden"},
            "authorization_header": "hidden",
            "host_path": "/tmp/hidden",
            "items": [{"cookie": "hidden", "name": "visible", "raw_logs": ["hidden"]}],
            "environment": {"REGION": "hidden"},
            "artifact": {"content": "hidden", "summary": "visible summary"},
            "generic_location": "/Users/example/private",
            "service_url": "https://user:password@example.test/resource",
        }
    )
    assert value == {
        "safe": {"region": "east"},
        "items": [{"name": "visible"}],
        "artifact": {"summary": "visible summary"},
        "generic_location": "<redacted-path>",
        "service_url": "<redacted-credential-url>",
    }


def test_lifecycle_state_projection_covers_running_failed_paused_and_waiting():
    active_job = {"status": "active"}
    assert context_state(active_job, None, []) == "never_run"
    assert context_state(active_job, {"status": "running"}, []) == "running"
    assert context_state(active_job, {"status": "paused"}, []) == "paused"
    assert context_state(active_job, {"status": "completed"}, []) == "idle"
    assert context_state(active_job, {"status": "failed"}, []) == "idle"
    assert context_state(active_job, None, [{"status": "active"}]) == "scheduled_waiting"
    assert context_state({"status": "archived"}, {"status": "running"}, []) == "archived"
