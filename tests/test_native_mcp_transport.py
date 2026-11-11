"""MCP sessions exercise only the shared native transport, never runtime imports."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mn_api.app import create_app
from mn_api.job_supervision import BoundJobSupervision, native_launch_authority
from mn_api.runtime_mcp import JobContextProvider, wire_value
from mn_sdk.errors import AppError
from mn_sdk.runtime_control import RuntimeControlClient
from tests.mcp_protocol_fake import MCPRuntime, _configure, _mcp_request


@pytest.fixture
def transport(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime, token="surface-secret")
    original = RuntimeControlClient.command
    settings = SimpleNamespace(mode="response", error=None, pending=None, calls=[])

    def command(self, action, values, **kwargs):
        settings.calls.append((action, values))
        if settings.error:
            raise settings.error
        if action == "mcp.context":
            method = values["method"]
            if method == "response_mode":
                return settings.mode
            if method == "get_pending_human_request":
                return settings.pending
            if method in {"ask_job", "get_job_turn", "response_work_command"}:
                return {"answer": "runtime answer", "request_id": "reply-one"}
            if method == "record_pending_human_response":
                return {"accepted": True}
        if action == "mcp.supervision" and values["method"] == "invoke":
            return {"status": "accepted", "job_id": values["job_id"]}
        return original(self, action, values, **kwargs)

    monkeypatch.setattr(RuntimeControlClient, "command", command)
    return settings


@pytest.mark.parametrize("mode", ["response", "agent"])
@pytest.mark.parametrize("background", [False, True])
def test_ask_job_routes_to_runtime_without_starting_a_run(transport, mode, background):
    transport.mode = mode
    with TestClient(create_app()) as api:
        response = _mcp_request(
            api,
            "job-1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "ask_job",
                    "arguments": {
                        "question": "What happened?",
                        "background": background,
                        "request_id": "reply-one",
                        "conversation_id": "conversation-one",
                    },
                },
            },
            token="surface-secret",
        )
    assert response.status_code == 200, response.text
    assert "runtime answer" in response.text
    calls = [v for a, v in transport.calls if a == "mcp.context"]
    method = "response_work_command" if background else "ask_job"
    call = next(v for v in calls if v["method"] == method)
    assert call["job_id"] == "job-1" and call["args"] == ["What happened?"]
    assert call["kwargs"]["request_id"] == "reply-one"
    assert not any(v["method"] == "start_run" for _, v in transport.calls)


def test_agent_turn_polling_uses_same_native_connection(transport):
    transport.mode = "agent"
    with TestClient(create_app()) as api:
        response = _mcp_request(
            api,
            "job-1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_job_turn", "arguments": {"turn_id": "reply-one"}},
            },
            token="surface-secret",
        )
    assert response.status_code == 200 and "runtime answer" in response.text
    assert any(v.get("method") == "get_job_turn" and v["args"] == ["reply-one"] for _, v in transport.calls)


@pytest.mark.parametrize("status, expected", [(404, 404), (503, 503)])
def test_mcp_connection_failures_preserve_structured_status(transport, status, expected):
    transport.error = AppError("MN_TEST_REMOTE", "Remote unavailable", http_status=status)
    with TestClient(create_app()) as api:
        response = _mcp_request(
            api,
            "job-1",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            token="surface-secret",
        )
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("application/problem+json")


def test_supervision_preserves_ephemeral_launch_input_and_authority(transport):
    supervision = BoundJobSupervision(JobContextProvider(), lambda: "job-1")
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            meta={
                "mn.launch": {"inputs": {"key": "private-input"}},
                "unrelated": "drop-me",
            }
        )
    )
    marker = native_launch_authority.set(True)
    try:
        result = asyncio.run(supervision.invoke("start_run", {"node": "node-two"}, ctx))
        assert result["status"] == "accepted"
        assert asyncio.run(supervision.capabilities())["available"] is True
    finally:
        native_launch_authority.reset(marker)
    launch = next(v for a, v in transport.calls if a == "mcp.supervision" and v.get("name") == "start_run")
    assert launch["internal"] is True
    assert launch["metadata"] == {"mn.launch": {"inputs": {"key": "private-input"}}}
    assert launch["arguments"] == {"node": "node-two"}


@pytest.mark.parametrize("status, error", [(404, KeyError), (422, ValueError), (409, AppError)])
def test_supervision_errors_keep_existing_mcp_contract(transport, status, error):
    transport.error = AppError("MN_REMOTE_REJECTED", "Rejected", http_status=status)
    with pytest.raises(error):
        BoundJobSupervision(JobContextProvider(), lambda: "job-1").identity()


def test_native_context_rejects_unknown_methods_and_serializes_models(transport):
    class Arguments(BaseModel):
        selection: tuple[str, ...]

    assert wire_value({"value": Arguments(selection=("one", "two"))}) == {"value": {"selection": ["one", "two"]}}
    with pytest.raises(AttributeError):
        JobContextProvider().execute_arbitrary_code
    transport.error = AppError("MN_REMOTE_CONFLICT", "Conflict", http_status=409)
    with pytest.raises(AppError, match="Conflict"):
        JobContextProvider().response_mode("job-1")


def test_pending_human_input_is_resolved_before_remote_tool(transport, monkeypatch):
    supervision = BoundJobSupervision(JobContextProvider(), lambda: "job-1")
    supervision.resolve_human_input = lambda ctx: {"pending": True}
    assert asyncio.run(supervision.invoke("get_job_context", {}, object())) == {"pending": True}
    assert transport.calls == []
    monkeypatch.setattr("mn_sdk_mcp.resolve_job_activity_receipt", lambda ctx: {"receipt": "observed"})
    assert asyncio.run(supervision.invoke("watch_job_activity", {}, object())) == {"receipt": "observed"}
    assert transport.calls == []


def test_human_input_request_and_response_preserve_runtime_request_identity(transport, monkeypatch):
    import mn_sdk_mcp
    from mn_api.job_mcp import create_job_mcp, _current_job_id

    original = mn_sdk_mcp.create_supervision_server
    captured = []

    def capture(name, supervision, **kwargs):
        captured.append(supervision)
        return original(name, supervision, **kwargs)

    monkeypatch.setattr(mn_sdk_mcp, "create_supervision_server", capture)
    create_job_mcp()
    resolve = captured[0].resolve_human_input
    transport.pending = {"request_id": "question-one", "prompt": "Choose a region", "options": ["east", "west"]}
    marker = _current_job_id.set("job-1")
    try:
        result = resolve(SimpleNamespace(request_state=None))
        request = result.input_requests["human-response:question-one"]
        assert request.params.requested_schema["properties"]["response"]["enum"] == ["east", "west"]
        assert resolve(SimpleNamespace(request_state=result.request_state, input_responses={})) is None
        assert (
            resolve(
                SimpleNamespace(
                    request_state=result.request_state,
                    input_responses={
                        "human-response:question-one": SimpleNamespace(action="accept", content={"response": "east"}),
                    },
                )
            )
            is None
        )
        recorded = next(v for _, v in transport.calls if v.get("method") == "record_pending_human_response")
        assert recorded["job_id"] == "job-1" and recorded["args"] == ["question-one", "east"]
        for malformed in ["invalid json", "{}"]:
            with pytest.raises(ValueError, match="request state"):
                resolve(SimpleNamespace(request_state=malformed))
    finally:
        _current_job_id.reset(marker)


def test_transport_timeout_does_not_resubmit_an_accepted_reply():
    from mn_api.job_mcp import _reply_rpc, JobMCPUnavailableError

    calls = []

    def unavailable():
        calls.append("observe")
        raise JobMCPUnavailableError("The runtime is temporarily unavailable")

    assert _reply_rpc(unavailable)["state"] == "unavailable"
    assert calls == ["observe"]
