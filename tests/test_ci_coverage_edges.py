from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mn_sdk.errors import AppError

from mn_api import agent_graph, job_activity, state
from mn_api.config import (
    ApiConfig,
    auth_enabled,
    config_bool,
    config_float,
    config_int,
    config_list,
    config_optional_value,
    config_path,
    config_string,
    effective_env_values,
    redacted_value,
    subprocess_environment,
)
from mn_api.logging_config import configure_logging
from mn_api.path_utils import inside_path
from mn_api.routes import bundles, deployments, realtime
from mn_api.web_ui_server import create_app, resolve_dist_dir
from mn_api.config_schema import ConfigError, parse_bool, parse_float, parse_int, parse_path, parse_url
from mn_api.dependencies import enforce_request_size, require_auth, require_websocket_auth


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def test_agent_graph_covers_manifest_fallbacks_and_observed_edges(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "agents": {
                    "nodes": [{"node_id": "declared", "role": "planner"}],
                    "edges": [{"from_node": "declared", "to_node": "worker", "message_type": "task"}],
                },
                "nodes": [{"node_id": "root-node"}],
                "metadata": {"agent_templates": {"nodes": [{"node_id": "template-node"}]}},
            }
        ),
        encoding="utf-8",
    )
    graph = agent_graph.build_agent_graph(
        "job-graph",
        {
            "job": {
                "status": "running",
                "manifest_ref": {"manifest_path": str(manifest_path)},
            },
            "summary": {"graph_id": "fallback-graph"},
            "agents": [
                {"agent_id": "runtime", "assigned_node": "unknown"},
                {"agent_id": "worker", "metadata": {"outbound_edges": ["runtime", "extra"]}},
            ],
        },
        [
            {"type": "delivery_failed", "payload": {"from": "worker", "to": "declared", "type": "failure"}},
            {"type": "other", "message": {"from": "declared", "to": "extra"}},
            {"type": "ignored", "payload": {"to": "missing-source"}},
            {"type": "ignored", "message": "not-an-object"},
        ],
    )

    nodes = {node["id"]: node for node in graph["nodes"]}
    assert graph["graph_id"] == "fallback-graph"
    assert nodes["runtime"]["assigned_node"] == "system/runtime"
    assert nodes["runtime"]["label"] == "System Runtime"
    assert nodes["declared"]["role"] == "planner"
    assert nodes["template-node"]["status"] == "declared"
    assert any(edge["source"] == "worker" and edge["target"] == "extra" for edge in graph["edges"])


def test_agent_graph_helpers_cover_invalid_manifests_and_labels(tmp_path):
    assert agent_graph._manifest_agent_nodes(None) == []
    assert agent_graph._manifest_agent_edges(None) == []
    assert agent_graph.event_message_summary({"type": "other", "message": {"from": "a"}}) == {"from": "a"}
    assert agent_graph.event_message_summary({"type": "other"}) is None
    assert agent_graph._graph_agent_label("runtime", {"label": "unknown"}) == "System Runtime"
    assert agent_graph._graph_assigned_node("runtime", {"assigned_node": "unassigned"}) == "system/runtime"
    assert agent_graph._graph_assigned_node("worker", {"assigned_node": "unknown"}) == "unassigned"

    valid = tmp_path / "valid.json"
    valid.write_text('{"graph_id": "g"}', encoding="utf-8")
    assert agent_graph.load_manifest_for_job({"manifest_ref": {"manifest_path": str(valid)}}) == {"graph_id": "g"}


def test_realtime_message_and_event_helpers_cover_error_paths(monkeypatch):
    websocket = FakeWebSocket()
    subscriptions = {}

    async def exercise():
        await realtime.handle_realtime_message(websocket, "not-json-object", subscriptions)
        await realtime.handle_realtime_message(
            websocket, {"action": "subscribe", "requestId": "bad", "topic": "unknown:1"}, subscriptions
        )
        await realtime.handle_realtime_message(
            websocket, {"action": "subscribe", "requestId": "req", "topic": "launch_progress:run", "after": "bad"}, subscriptions
        )
        await realtime.handle_realtime_message(
            websocket, {"action": "unsubscribe", "requestId": "req", "topic": "launch_progress:run"}, subscriptions
        )
        await realtime.handle_realtime_message(websocket, {"action": "unknown", "topic": "run"}, subscriptions)

    monkeypatch.setattr(realtime.blueprints, "validate_progress_id", lambda value: value)
    asyncio.run(exercise())

    assert websocket.sent[0]["code"] == "INVALID_MESSAGE"
    assert websocket.sent[1]["code"] == "TOPIC_NOT_FOUND"
    assert websocket.sent[2]["action"] == "subscribed"
    assert websocket.sent[3]["action"] == "unsubscribed"
    assert websocket.sent[4]["code"] == "INVALID_MESSAGE"
    assert realtime.valid_realtime_topic("launch_progress:run") is True
    assert realtime.valid_realtime_topic("jobs:run") is False
    assert realtime.int_value("bad", default=7) == 7
    assert realtime.realtime_events_after("jobs:run", 0) == []

    monkeypatch.setattr(
        realtime.blueprints,
        "launch_progress_snapshot",
        lambda _progress_id: {
            "status": "running",
            "current_phase": "launch",
            "events": [{"phase": "launch", "status": "running"}, "bad", {"status": "done", "ts": "now"}],
        },
    )
    events = realtime.launch_progress_events_after("run", 0)
    assert len(events) == 2
    assert events[0]["type"] == "blueprint.launch_progress.launch.running"
    assert len(realtime.launch_progress_events_after("run", 2)) == 1


def test_configuration_helpers_cover_defaults_and_sensitive_values(monkeypatch, tmp_path):
    source = SimpleNamespace(effective_env={"MN_API_PORT": "54002", "MN_API_TOKEN": "secret"})
    assert config_string("MN_API_HOST", source=source, default=" localhost ") == "localhost"
    assert config_int("MN_API_PORT", source=source, default=1) == 54002
    assert config_float("UNSPEC_FLOAT", source=source, default=1.5) == 1.5
    assert config_bool("MN_RUN_BACKGROUND_EVENT_RELAY", runtime_env={"MN_RUN_BACKGROUND_EVENT_RELAY": "false"}) is False
    assert config_list("MN_API_CORS_ALLOW_ORIGINS", runtime_env={"MN_API_CORS_ALLOW_ORIGINS": "a, b"}) == ["a", "b"]
    assert config_path("MN_API_LOG_PATH", source=SimpleNamespace(effective_env={}), default=tmp_path / "api.log") == tmp_path / "api.log"
    assert config_optional_value("MN_API_TOKEN", source=source) == "secret"
    assert config_optional_value("MN_API_TOKEN", source=SimpleNamespace(effective_env={}), runtime_env={"MN_API_TOKEN": "runtime"}) == "runtime"
    assert config_optional_value("MN_API_TOKEN", source=SimpleNamespace(effective_env={})) is None
    assert redacted_value("MN_API_TOKEN", "secret") == "<redacted>"
    assert redacted_value("MN_API_HOST", "localhost") == "localhost"
    assert auth_enabled(ApiConfig.from_env(env={"MN_API_TOKEN": "token"})) is True
    monkeypatch.setenv("MN_LOG_LEVEL", "DEBUG")


def test_deployment_and_bundle_edge_handlers(monkeypatch, tmp_path):
    with pytest.raises(HTTPException) as raised:
        deployments._manifest_and_payloads(SimpleNamespace(bundle_path=None, manifest_json=None, payloads=None))
    assert raised.value.status_code == 422

    class FailingRuntime:
        def list_deployments(self):
            raise RuntimeError("runtime down")

    monkeypatch.setattr(state, "client", SimpleNamespace())
    monkeypatch.setattr(deployments, "RuntimeService", lambda _client: FailingRuntime())
    response = deployments.list_deployments()
    assert response.status_code == 500

    class Upload:
        filename = "bundle.zip"
        content_type = "application/zip"

    async def save(_bundle, _root):
        return {"bundle_path": str(tmp_path / "bundle")}

    monkeypatch.setattr(bundles, "save_uploaded_bundle", save)
    state.client.emit_trigger_event = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event down"))
    result = asyncio.run(bundles.upload_bundle(Upload()))
    assert result["bundle_path"].endswith("bundle")


def test_deployment_action_success_paths(monkeypatch):
    class Runtime:
        def list_deployments(self): return {"action": "list"}
        def get_deployment(self, value): return {"action": "get", "id": value}
        def promote_deployment(self, value): return {"action": "promote", "id": value}
        def rollback_deployment(self, value, **kwargs): return {"action": "rollback", "id": value, **kwargs}
        def pause_deployment(self, value, **kwargs): return {"action": "pause", "id": value, **kwargs}
        def resume_deployment(self, value, **kwargs): return {"action": "resume", "id": value, **kwargs}
        def fail_deployment(self, value, **kwargs): return {"action": "fail", "id": value, **kwargs}

    monkeypatch.setattr(deployments, "RuntimeService", lambda _client: Runtime())
    monkeypatch.setattr(state, "client", object())
    assert deployments.list_deployments() == {"action": "list"}
    assert deployments.get_deployment("d") == {"action": "get", "id": "d"}
    assert deployments.promote_deployment("d") == {"action": "promote", "id": "d"}
    assert deployments.rollback_deployment("d", SimpleNamespace(version="v2", tag="stable", reason="bad"))["tag"] == "stable"
    assert deployments.pause_deployment("d", None)["reason"] == ""
    assert deployments.resume_deployment("d", SimpleNamespace(reason="done"))["reason"] == "done"
    assert deployments.fail_deployment("d", None)["reason"] == ""

    class BrokenRuntime:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("runtime down"))

    monkeypatch.setattr(deployments, "RuntimeService", lambda _client: BrokenRuntime())
    assert deployments.list_deployments().status_code == 500
    assert deployments.get_deployment("d").status_code == 500
    assert deployments.promote_deployment("d").status_code == 500
    assert deployments.rollback_deployment("d", SimpleNamespace(version="v2", tag="stable", reason="bad")).status_code == 500
    assert deployments.pause_deployment("d", None).status_code == 500
    assert deployments.resume_deployment("d", None).status_code == 500
    assert deployments.fail_deployment("d", None).status_code == 500


def test_logging_paths_and_static_ui_fallbacks(monkeypatch, tmp_path):
    logger_name = "mn-api-ci-coverage"
    monkeypatch.setenv("MN_LOGS_ROOT", str(tmp_path / "logs"))
    logger = configure_logging(logger_name, default_file="ci.log")
    assert logger.name == logger_name
    assert inside_path(tmp_path / "child", tmp_path)
    assert not inside_path(tmp_path.parent, tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<html />", encoding="utf-8")
    assert resolve_dist_dir(cwd=tmp_path) == tmp_path / "dist"
    assert resolve_dist_dir(str(tmp_path)) == tmp_path

    app = create_app(dist_dir=tmp_path / "missing", api_url="http://api.local/api/v1")
    response = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app).get("/health")
    assert response.json()["status"] == "missing"


def test_remaining_small_utility_branches(monkeypatch, tmp_path):
    logger = configure_logging("mn-api-ci-reuse", default_file="ci.log")
    assert configure_logging("mn-api-ci-reuse", default_file="ci.log") is logger

    def fail_handler(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr("mn_api.logging_config.RotatingFileHandler", fail_handler)
    fallback = configure_logging("mn-api-ci-fallback", default_file="fallback.log")
    assert fallback.handlers[0].__class__.__name__ == "StreamHandler"

    async def bad_zip(_bundle, _root):
        import zipfile
        raise zipfile.BadZipFile("bad")

    monkeypatch.setattr(bundles, "save_uploaded_bundle", bad_zip)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(bundles.upload_bundle(SimpleNamespace(filename="bad.zip", content_type="application/zip")))
    assert raised.value.status_code == 400

    assert subprocess_environment({"EXTRA": 1})["EXTRA"] == "1"
    assert isinstance(effective_env_values(), dict)
    assert job_activity.compact_value(object())
    assert job_activity._compact_activity_event({"type": "tool_call", "payload": {"duration_ms": 1}})["duration_ms"] == 1

    monkeypatch.setattr(state, "config", SimpleNamespace(cors_allow_origins=["*"]))
    from mn_api.app import create_app
    assert create_app().title == "MirrorNeuron API"


def test_job_activity_compaction_and_workflow_enrichment_paths(monkeypatch):
    assert job_activity.compact_value("x" * 2100)["truncated"] is True
    assert job_activity.compact_value(b"abc")["type"] == "bytes"
    assert job_activity.compact_value(list(range(30)))[-1]["omitted_items"] == 5
    assert job_activity.compact_value({"logs": "secret", "nested": {"value": 1}})["logs"]["omitted"] is True
    assert job_activity.compact_value({"x": "deep"}, depth=6)["omitted"] is True
    assert job_activity._compact_blob(b"abc")["type"] == "bytes"
    assert job_activity._compact_blob([1])["type"] == "array"
    assert job_activity._compact_blob(object())["type"] == "object"
    assert job_activity._compact_activity_text("word " * 100, limit=20, prefer_tail=True).startswith("[truncated]")
    assert job_activity._event_category({"type": "tool_call"}, {}) == "tool"
    assert job_activity._event_category({"type": "artifact_written"}, {}) == "artifact"
    assert job_activity._event_category({"type": "workflow_step_started"}, {}) == "system"
    assert job_activity._event_category({"type": "agent_activity"}, {}) == "agent"
    assert job_activity._activity_message({"type": "docker_worker_build_started"}) == "DockerWorker image build started"
    assert job_activity._activity_message({"type": "docker_worker_build_completed"}) == "DockerWorker image build completed"
    assert job_activity._activity_message({"type": "docker_worker_build_failed"}) == "DockerWorker image build failed"
    assert job_activity._activity_message({"type": "docker_worker_command_started"}) == "DockerWorker command started"
    assert job_activity._activity_message({"type": "docker_worker_command_completed"}) == "DockerWorker command completed"
    assert job_activity._activity_message({"type": "docker_worker_command_timed_out"}) == "DockerWorker command timed out"
    assert job_activity._activity_message({"type": "workflow_worker_started", "agent_id": "a"}) == "Agent working: a"
    assert job_activity._activity_message({"type": "workflow_step_completed", "step_id": "s"}) == "Step completed: s"
    assert job_activity._activity_message({"type": "workflow_step_attempt_retry_scheduled", "step_id": "s"}) == "Retry pending: s"
    assert job_activity._activity_message({"type": "workflow_step_blocked", "step_id": "s"}) == "Blocked: s"
    monkeypatch.setattr(job_activity, "failure_from_event", lambda _event: {"desc": "failed"})
    assert job_activity._activity_message({"type": "failed"}) == "failed"
    assert job_activity._agent_step_id({"node:a": "step"}, "a") == "step"
    assert job_activity._agent_step_id({"node:a": "step"}, "other") == ""
    assert job_activity._agent_step_id({}, "") == ""

    snapshot = {
        "steps": [{"id": "step-1", "agents": [{"id": "agent-1"}, {"id": "agent-2"}]}, {"id": "step-2"}],
        "current_step": {"id": "step-1", "current": True},
    }
    events = [
        {"type": "workflow_step_started", "payload": {"step": "step-1", "worker": "agent-1"}},
        {
            "type": "tool_call",
            "payload": {"step_id": "step-1", "agent_id": "agent-1", "tool_name": "search", "details": {"q": "x"}},
        },
        {"type": "workflow_step_completed", "payload": {"step": "step-1", "worker": "agent-1", "status": "done"}},
        {"type": "ignored", "payload": {"step": "missing"}},
        "invalid",
    ]
    job_activity.enrich_workflow_progress_activity(snapshot, events)
    assert snapshot["steps"][0]["activity_summary"]
    assert snapshot["steps"][0]["agents"][0]["recent_events"]
    assert snapshot["current_step"]["id"] == "step-1"
    empty = {"steps": [], "current_step": {"id": "none"}}
    job_activity.enrich_workflow_progress_activity(empty, [])
    assert empty["steps"] == []
    odd = {"steps": ["bad", {}, {"id": "step", "agents": ["bad", {}]}], "current_step": {"id": "unknown"}}
    job_activity.enrich_workflow_progress_activity(odd, [{"type": "ignored", "payload": {"step": "unknown"}}])
    assert odd["current_step"]["id"] == "unknown"


def test_config_schema_parsers_and_request_guards(monkeypatch):
    assert parse_int("port", " 12 ") == 12
    assert parse_float("timeout", " 1.5 ") == 1.5
    assert parse_bool("flag", "yes") is True
    assert parse_bool("flag", "off") is False
    assert parse_url("url", "https://example.test/") == "https://example.test"
    assert parse_path("path", "~/tmp").name == "tmp"
    with pytest.raises(ConfigError):
        parse_int("port", "bad")
    with pytest.raises(ConfigError):
        parse_float("timeout", "bad")
    with pytest.raises(ConfigError):
        parse_bool("flag", "maybe")
    with pytest.raises(ConfigError):
        parse_url("url", "not-a-url")
    with pytest.raises(ConfigError):
        parse_path("path", "")

    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="token", request_size_limit_bytes=10))
    assert require_auth("Bearer token") is None
    with pytest.raises(HTTPException):
        require_auth("Bearer wrong")

    request = SimpleNamespace(headers={"content-length": "bad"})
    invalid_length = asyncio.run(enforce_request_size(request, AsyncMock()))
    assert invalid_length.status_code == 400
    request.headers = {"content-length": "11"}
    too_large = asyncio.run(enforce_request_size(request, AsyncMock()))
    assert too_large.status_code == 413
    request.headers = {}
    next_handler = AsyncMock(return_value="ok")
    assert asyncio.run(enforce_request_size(request, next_handler)) == "ok"

    websocket = SimpleNamespace(headers={"authorization": "Bearer token"}, query_params={})
    asyncio.run(require_websocket_auth(websocket))


def test_error_handlers_cover_request_and_malformed_validation_paths():
    from mn_api.app import create_app
    from mn_api.errors import _human_detail, _validation_report_from_prefixed_detail

    assert _validation_report_from_prefixed_detail("other", "prefix:") is None
    assert _validation_report_from_prefixed_detail("prefix: {bad", "prefix:") is None
    assert _human_detail("plain", "prefix:") == "plain"

    app = create_app()

    @app.get("/empty-http-error")
    def empty_http_error():
        raise HTTPException(status_code=500, detail="")

    @app.get("/app-error")
    def app_error():
        raise AppError("MN_TEST_ERROR", "test failure", hint="retry", http_status=409)

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/empty-http-error", headers={"x-correlation-id": "corr-1"}).status_code == 500
    app_response = client.get("/app-error", headers={"x-request-id": "req-1"})
    assert app_response.status_code == 409
    assert app_response.json()["request_id"] == "req-1"
