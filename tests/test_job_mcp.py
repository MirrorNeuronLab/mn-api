from __future__ import annotations

import json
import threading
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
            "job-response": {
                "job_id": "job-response",
                "blueprint_id": "response-blueprint",
                "status": "active",
                "run_count": 0,
                "response_service": {"state": "ready", "ready_at": "2026-08-20T12:00:00Z"},
            },
            "job-response-disabled": {
                "job_id": "job-response-disabled",
                "blueprint_id": "response-blueprint",
                "status": "active",
                "response_service": {"state": "disabled"},
            },
            "job-agent": {
                "job_id": "job-agent",
                "blueprint_id": "agent-blueprint",
                "status": "active",
                "latest_run_id": "run-agent",
                "response_service": {"state": "ready"},
            },
        }
        self.runs: dict[str, list[dict]] = {
            "job-1": [],
            "job-2": [],
            "job-response": [],
            "job-agent": [
                {
                    "job_id": "job-agent",
                    "run_id": "run-agent",
                    "runtime_run_id": "runtime-run-agent",
                    "status": "running",
                }
            ],
        }
        self.queries: list[dict] = []
        self.get_job_calls = 0
        self.list_run_calls = 0

    def get_job(self, job_id):
        self.get_job_calls += 1
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="missing")
        return json.dumps(self.jobs[job_id])

    def list_runs(self, job_id, *, page_size=50, page_token=""):
        self.list_run_calls += 1
        del page_size, page_token
        return json.dumps({"items": self.runs.get(job_id, [])})

    def get_run(self, run_id):
        for run_items in self.runs.values():
            for run in run_items:
                if run.get("run_id") == run_id:
                    return json.dumps(run)
        raise HTTPException(status_code=404, detail="missing")

    def query_job_response(
        self,
        job_id,
        question,
        *,
        context,
        conversation_id="",
        request_id="",
    ):
        self.queries.append({"job_id": job_id, "question": question, "context": context})
        return json.dumps(
            {
                "schema_version": "mn.mcp.job_answer.v1",
                "answer": "This Job is ready and has not started a Run.",
                "conversation_id": conversation_id or "57a8b9f2-23c8-4eef-8e2d-14806fb63739",
                "request_id": request_id or None,
                "job_id": job_id,
                "state": {"job": context["state"], "latest_run": None},
                "citations": [],
                "warnings": [],
                "service": {"state": "ready"},
                "model": {"provider": "fake", "model": "fast", "used": True, "fallback": False},
                "conversation_persisted": True,
            }
        )

    def get_job_response_turn(self, job_id, turn_id):
        return json.dumps(
            {
                "schema_version": "mn.mcp.job_answer.v3",
                "answer": "Reached Zone A.",
                "conversation_id": "57a8b9f2-23c-4eef-8e2d-14806fb63739",
                "request_id": "request-agent",
                "job_id": job_id,
                "state": {"job": "running", "latest_run": None},
                "citations": [],
                "warnings": [],
                "service": {"state": "ready"},
                "model": {"used": False, "fallback": False},
                "conversation_persisted": True,
                "turn": {"turn_id": turn_id, "state": "completed"},
                "effects": [
                    {
                        "kind": "bounded_tool",
                        "tool": "process_item",
                        "effect": "write",
                        "arguments": {"item_id": "item-1"},
                        "state": "completed",
                    }
                ],
            }
        )

def _blueprint(_config, blueprint_id):
    enabled = blueprint_id != "disabled-blueprint"
    blueprint = {
        "id": blueprint_id,
        "name": "Queue Reviewer",
        "description": "Review the account queue and explain the latest result.",
        "capabilities": ["Review", "Explain"],
        "response_service": {
            "enabled": enabled,
            "goal_id": "goal-1",
        },
    }
    if blueprint_id == "response-blueprint":
        blueprint["response_service"] = {"enabled": True}
    if blueprint_id == "agent-blueprint":
        blueprint["response_service"] = {
            "enabled": True,
            "agent": {"kind": "bounded_mcp"},
        }
    return "catalog", blueprint


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
            "ask_job",
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


def test_response_enabled_job_lists_ask_job_and_never_starts_a_run(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)

    with TestClient(create_app()) as client:
        assert _initialize(client, "job-response").status_code == 200
        listed = _mcp_request(
            client,
            "job-response",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "ask_job",
            "get_job_context",
            "get_job_profile",
            "get_latest_run",
        }
        called = _mcp_request(
            client,
            "job-response",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ask_job",
                    "arguments": {
                        "question": "What is happening?",
                        "request_id": "request-1",
                    },
                },
            },
        ).json()["result"]["structuredContent"]

    assert called["schema_version"] == "mn.mcp.job_answer.v1"
    assert called["state"]["job"] == "never_run"
    assert runtime.jobs["job-response"]["run_count"] == 0
    assert runtime.runs["job-response"] == []
    assert len(runtime.queries) == 1


def test_agent_enabled_job_adds_turn_polling_and_preserves_internal_service_run_id(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)
    turn_id = "fcb09ddb-35a7-4a40-9ce0-14f25093a6db"

    original_query = runtime.query_job_response

    def agent_query(job_id, question, **kwargs):
        context = kwargs["context"]
        assert context["_active_service_run_id"] == "runtime-run-agent"
        payload = json.loads(original_query(job_id, question, **kwargs))
        payload.update(
            {
                "schema_version": "mn.mcp.job_answer.v3",
                "turn": {"turn_id": turn_id, "state": "accepted", "poll_after_ms": 1000},
                "effects": [
                    {
                        "kind": "bounded_tool",
                        "tool": "process_item",
                        "effect": "write",
                        "arguments": {"item_id": "item-1"},
                        "state": "accepted",
                    }
                ],
            }
        )
        return json.dumps(payload)

    runtime.query_job_response = agent_query

    with TestClient(create_app()) as client:
        assert _initialize(client, "job-agent").status_code == 200
        listed = _mcp_request(
            client,
            "job-agent",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "ask_job",
            "get_job_context",
            "get_job_profile",
            "get_job_turn",
            "get_latest_run",
        }
        accepted = _mcp_request(
            client,
            "job-agent",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ask_job",
                    "arguments": {"question": "Move to Zone A", "request_id": "request-agent"},
                },
            },
        ).json()["result"]["structuredContent"]
        completed = _mcp_request(
            client,
            "job-agent",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_job_turn", "arguments": {"turn_id": turn_id}},
            },
        ).json()["result"]["structuredContent"]

    assert accepted["schema_version"] == "mn.mcp.job_answer.v3"
    assert accepted["turn"]["state"] == "accepted"
    assert completed["answer"] == "Reached Zone A."


def test_job_projection_is_authoritative_when_catalog_now_enables_responses(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)

    with TestClient(create_app()) as client:
        assert _initialize(client, "job-response-disabled").status_code == 404


def test_ask_job_preserves_idempotency_conflicts_instead_of_falling_back(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)

    def conflict(*_args, **_kwargs):
        raise RuntimeError("ALREADY_EXISTS: request_id_conflict")

    runtime.query_job_response = conflict

    try:
        JobContextProvider().ask_job(
            "job-response",
            "Different question",
            request_id="request-1",
        )
    except ValueError as error:
        assert "different question" in str(error)
    else:
        raise AssertionError("Expected request_id conflict to be preserved")


def test_agent_failure_returns_generic_v3_fallback(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("response unavailable")

    runtime.query_job_response = unavailable
    response = JobContextProvider().ask_job("job-agent", "What is the current status?")

    assert response["schema_version"] == "mn.mcp.job_answer.v3"
    assert response["turn"]["state"] == "completed"
    assert response["effects"] == []


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


def test_context_resolves_final_result_from_staged_runtime_output_identity(monkeypatch):
    runtime = MCPRuntime()
    runtime.jobs["job-1"]["latest_run_id"] = "public-run-1"
    runtime.runs["job-1"] = [
        {
            "job_id": "job-1",
            "run_id": "public-run-1",
            "status": "completed",
            "result_ref": {
                "version": "mn.staged_artifact/v1",
                "type": "artifact_ref",
                "storage": "syncthing",
                "submission_id": "job-definition-1",
                "run_id": "runtime-output-1",
                "relative_path": "outputs/runs/runtime-output-1/artifacts/final.json",
                "sha256": "a" * 64,
                "size_bytes": 128,
            },
        }
    ]
    _configure(monkeypatch, runtime)
    projected_ids: list[str] = []
    resolved_references: list[dict] = []
    monkeypatch.setattr(
        job_mcp.runtime_run_routes,
        "get_run_final_artifact",
        lambda run_id, _principal: projected_ids.append(run_id) or (_ for _ in ()).throw(
            HTTPException(status_code=404, detail="runtime final file not copied")
        ),
    )
    monkeypatch.setattr(
        job_mcp,
        "resolve_json_reference",
        lambda reference, **_kwargs: resolved_references.append(reference) or {"summary": "Published result"},
    )

    context = JobContextProvider().get_context("job-1")

    assert projected_ids == ["runtime-output-1"]
    assert resolved_references == [runtime.runs["job-1"][0]["result_ref"]]
    assert context["latest_run"]["run_id"] == "public-run-1"
    assert context["latest_run"]["result"] == {"summary": "Published result"}
    assert any(
        item["record_id"] == "final-result" and item["publication_state"] == "final"
        for item in context["evidence"]
    )


def test_response_service_requires_exact_top_level_snake_case_declaration():
    assert job_mcp._response_service_declared({"response_service": {"enabled": True}}) is True
    assert job_mcp._response_service_declared({"responseService": {"enabled": True}}) is False
    assert job_mcp._response_service_declared({"metadata": {"response_service": {"enabled": True}}}) is False


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
            "service_source": "https://user:password@example.test/resource",
        }
    )
    assert value == {
        "safe": {"region": "east"},
        "items": [{"name": "visible"}],
        "artifact": {"summary": "visible summary"},
        "generic_location": "<redacted-path>",
        "service_source": "<redacted-credential-url>",
    }


def test_ask_job_reuses_one_job_and_run_snapshot(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)
    provider = JobContextProvider()

    response = provider.ask_job("job-agent", "Can you move to Zone B?")

    assert response["schema_version"] == "mn.mcp.job_answer.v1"
    assert runtime.get_job_calls == 1
    assert runtime.list_run_calls == 1
    assert runtime.queries[0]["context"]["_active_service_run_id"] == "runtime-run-agent"


def test_context_returns_marked_last_known_good_while_one_refresh_runs(monkeypatch):
    runtime = MCPRuntime()
    runtime.jobs["job-1"]["latest_run_id"] = "run-1"
    runtime.runs["job-1"] = [{"job_id": "job-1", "run_id": "run-1", "status": "running"}]
    _configure(monkeypatch, runtime)
    now = [0.0]
    provider = JobContextProvider(clock=lambda: now[0], initial_wait_seconds=0.5)
    first = provider.get_context("job-1")
    assert first["freshness"]["state"] == "fresh"

    release = threading.Event()
    original_list_runs = runtime.list_runs

    def blocked_list_runs(*args, **kwargs):
        release.wait(1)
        return original_list_runs(*args, **kwargs)

    runtime.list_runs = blocked_list_runs
    now[0] = 3.0
    stale = provider.get_context("job-1")
    second = provider.get_context("job-1")

    assert stale["freshness"] == {
        "state": "last_known_good",
        "fetched_at": first["fetched_at"],
        "age_ms": 3000,
        "max_age_ms": 30_000,
        "source": "cache",
        "refresh_in_progress": True,
    }
    assert second["freshness"]["state"] == "last_known_good"
    assert runtime.list_run_calls == 1
    release.set()


def test_initial_slow_snapshot_is_marked_unavailable_without_blocking(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)
    release = threading.Event()
    original_list_runs = runtime.list_runs

    def blocked_list_runs(*args, **kwargs):
        release.wait(1)
        return original_list_runs(*args, **kwargs)

    runtime.list_runs = blocked_list_runs
    provider = JobContextProvider(initial_wait_seconds=0.01)

    context = provider.get_context("job-1")

    assert context["state"] == "unknown"
    assert context["freshness"]["state"] == "unavailable"
    assert context["freshness"]["refresh_in_progress"] is True
    release.set()


def test_job_configuration_change_invalidates_cached_context(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)
    now = [0.0]
    provider = JobContextProvider(clock=lambda: now[0])

    first = provider.get_context("job-1")
    assert first["profile"]["configuration"]["query"] == "safe"

    runtime.jobs["job-1"]["resolved_configuration"]["query"] = "updated"
    now[0] = 3.0
    updated = provider.get_context("job-1")

    assert updated["profile"]["configuration"]["query"] == "updated"
    assert updated["freshness"]["state"] == "fresh"
    assert runtime.get_job_calls == 2
    assert runtime.list_run_calls == 2


def test_lifecycle_state_projection_covers_running_failed_paused_and_waiting():
    active_job = {"status": "active"}
    assert context_state(active_job, None, []) == "never_run"
    assert context_state(active_job, {"status": "running"}, []) == "running"
    assert context_state(active_job, {"status": "paused"}, []) == "paused"
    assert context_state(active_job, {"status": "completed"}, []) == "idle"
    assert context_state(active_job, {"status": "failed"}, []) == "idle"
    assert context_state(active_job, None, [{"status": "active"}]) == "scheduled_waiting"
    assert context_state({"status": "archived"}, {"status": "running"}, []) == "archived"
