"""The REST surface adapts a native runtime connection without hosting it."""

import json
import os
from contextlib import contextmanager
from dataclasses import replace
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mn_api import state
from mn_api.app import create_app
from mn_sdk.errors import AppError
from mn_sdk.proto import runtime_control_pb2 as wire
from mn_sdk.runtime_connection import REQUIRED_CAPABILITIES, RuntimeConnection, load_connection, save_connection
from mn_sdk.runtime_control import RuntimeControlClient
from mn_sdk.job_ui_proxy import close_frame


def reply(value=None, status=200, **headers):
    return wire.RuntimeReply(version=1, status=status, value_json=json.dumps(value), metadata=headers)


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("MN_HOME", str(tmp_path))
    monkeypatch.setenv("MN_API_TOKEN", "local-secret")
    (tmp_path / "install_metadata.json").write_text('{"install_role":"client"}')
    monkeypatch.setattr(state, "config", replace(state.config, api_token="local-secret"))
    calls, closed = [], []
    config = SimpleNamespace(response=reply({"items": []}), frames=None, error=None, ui=None)

    def init(self, connection=None, **kwargs):
        self.connection = connection or load_connection()
        self.channel = SimpleNamespace(close=lambda: closed.append("channel"))

    def call(self, method, action, arguments=None, **kwargs):
        calls.append((method, action, arguments or {}, kwargs, self.connection))
        assert self.connection.token != "local-secret"
        if self.connection.token == "bad":
            raise AppError("MN_RUNTIME_UNAUTHENTICATED", "The runtime credential was rejected.", http_status=401)
        if action == "runtime.capabilities":
            return reply(
                {
                    "runtime_control_version": 1,
                    "runtime_id": "runtime-one",
                    "node_name": "node@remote",
                    "capabilities": sorted(REQUIRED_CAPABILITIES),
                }
            )
        if action == "node.remove":
            if arguments["node_name"] == "busy@remote":
                raise AppError("MN_NODE_DRAIN_REQUIRED", "Drain this node first.", http_status=409)
            return reply({"status": "removed"})
        if config.error:
            raise config.error
        return config.response

    @contextmanager
    def stream(self, method, action, arguments):
        calls.append((method, action, arguments, {}, self.connection))
        try:
            yield iter(config.frames or [])
        finally:
            closed.append("stream")

    def upload(self, action, arguments, chunks):
        calls.append(("Upload", action, arguments, b"".join(chunks), self.connection))
        return config.response

    @contextmanager
    def job_ui(self, frames):
        def incoming():
            metadata = next(frames)
            values = json.loads(metadata.request.arguments_json)
            calls.append(("JobUi", metadata.request.action, values, {}, self.connection))
            assert "authorization" not in values["headers"]
            if values.get("websocket"):
                yield wire.RuntimeFrame(
                    version=1,
                    kind="accepted",
                    reply=reply(None, 101, subprotocol="chat" if "chat" in values.get("subprotocols", []) else ""),
                )
                for frame in frames:
                    if frame.kind == "close":
                        return
                    if frame.data == b"close-worker":
                        yield close_frame(4001, "worker finished")
                        return
                    yield frame
            else:
                yield wire.RuntimeFrame(
                    version=1,
                    kind="headers",
                    reply=reply(
                        None,
                        206,
                        **{
                            "content-type": "application/octet-stream",
                            "content-range": "bytes 2-5/10",
                            "x-mn-set-cookies": json.dumps(["one=1; HttpOnly", "two=2"]),
                        },
                    ),
                )
                body = b"".join(frame.data for frame in frames)
                yield wire.RuntimeFrame(version=1, kind="data", data=body or b"2345")

        try:
            yield incoming()
        finally:
            closed.append("ui")

    for name, func in [
        ("__init__", init),
        ("call", call),
        ("stream", stream),
        ("upload_reply", upload),
        ("job_ui", job_ui),
    ]:
        monkeypatch.setattr(RuntimeControlClient, name, func)
    return SimpleNamespace(home=tmp_path, calls=calls, closed=closed, config=config)


def connected():
    save_connection(RuntimeConnection("remote:55051", "remote-secret", "node@remote", "runtime-one"))


def client():
    return TestClient(create_app(), headers={"Authorization": "Bearer local-secret"})


def test_health_and_connection_credentials_are_local(native):
    with client() as api:
        assert api.get("/api/v1/health", headers={"Authorization": ""}).json()["status"] == "ok"
        assert not native.calls
        assert api.get("/api/v1/jobs", headers={"Authorization": "Bearer remote-secret"}).status_code == 401
        assert api.get("/api/v1/jobs").status_code == 503
        added = api.post("/api/v1/nodes", json={"host": "remote", "token": "remote-secret"})
        assert added.status_code == 201 and "remote-secret" not in added.text
        assert added.headers["location"] == "/api/v1/nodes/node@remote"
        before = (native.home / "client.json").read_bytes()
        assert api.post("/api/v1/nodes", json={"host": "replacement", "token": "bad"}).status_code == 401
        assert (native.home / "client.json").read_bytes() == before
        assert api.delete("/api/v1/nodes/busy@remote").status_code == 409
        assert (native.home / "client.json").read_bytes() == before
        assert api.delete("/api/v1/nodes/node@remote").status_code == 200
        assert not (native.home / "client.json").exists()
    assert all(call[4].token != "local-secret" for call in native.calls)


@pytest.mark.parametrize("status", [202, 400, 401, 409, 412, 422, 503])
def test_structured_reply_status_headers_and_request_options(native, status):
    connected()
    native.config.response = reply(
        {"run_id": "r1"} if status < 400 else {"code": "conflict", "detail": "Check the operation."},
        status,
        **{
            "content-type": "application/json" if status < 400 else "application/problem+json",
            "etag": '"rev-3"',
            "location": "/api/v1/runs/r1",
            "retry-after": "2",
        },
    )
    with client() as api:
        response = api.post(
            "/api/v1/blueprints/bp/runs",
            json={"admission_policy": "enqueue_if_busy", "owner_node": "worker", "job_id": "j1"},
            headers={"Idempotency-Key": "submission-1"},
        )
        assert response.status_code == status and response.json() == json.loads(native.config.response.value_json)
        assert response.headers["etag"] == '"rev-3"' and response.headers["location"] == "/api/v1/runs/r1"
        assert response.headers["retry-after"] == "2"
    method, action, args, kw, connection = native.calls[-1]
    assert method == "Prepare" and action == "create_blueprint_run"
    assert args["request"]["admission_policy"] == "enqueue_if_busy" and args["request"]["owner_node"] == "worker"
    assert kw["request_id"] == "submission-1" and "authorization" not in args["_context"].get("headers", {})


@pytest.mark.parametrize(
    "path,content_type,data,status",
    [
        ("/runs/r1/events/stream", "text/event-stream", b"id: event-1\nevent: progress\ndata: {}\n\n", 200),
        ("/runs/r1/outputs/0", "application/octet-stream", b"2345", 206),
    ],
)
def test_streaming_ranges_and_events_close_native_rpc(native, path, content_type, data, status):
    connected()
    native.config.frames = [
        wire.RuntimeFrame(
            version=1,
            kind="headers",
            reply=reply(
                None, status, **{"content-type": content_type, "content-range": "bytes 2-5/10", "etag": '"rev"'}
            ),
        ),
        wire.RuntimeFrame(version=1, kind="data", data=data),
    ]
    with client() as api:
        response = api.get("/api/v1" + path, headers={"Range": "bytes=2-5", "Last-Event-ID": "event-0"})
        assert response.status_code == status and response.content == data
    assert "stream" in native.closed
    if "events" in path:
        assert native.calls[-1][2]["last_event_id"] == "event-0"
    else:
        assert native.calls[-1][2]["_stream"]["headers"]["range"] == "bytes=2-5"


def test_empty_stream_is_a_protocol_error_and_closes(native):
    connected()
    with client() as api:
        response = api.get("/api/v1/runs/r1/outputs/0")
        assert response.status_code == 502 and response.json()["code"] == "MN_RUNTIME_PROTOCOL_ERROR"
    assert "stream" in native.closed


def test_reconnection_changes_next_request_and_failure_is_not_retried(native):
    connected()
    with client() as api:
        assert api.get("/api/v1/nodes").status_code == 200
        save_connection(RuntimeConnection("new:55052", "new-token", "node@new", "new-runtime"))
        assert api.get("/api/v1/nodes").status_code == 200
        assert [call[4].target for call in native.calls] == ["remote:55051", "new:55052"]
        native.config.error = AppError(
            "MN_RUNTIME_UNAVAILABLE", "The runtime is unavailable.", internal_message="private-token", http_status=503
        )
        response = api.post("/api/v1/blueprints/bp/runs", json={})
        assert response.status_code == 503 and "private-token" not in response.text
    assert len(native.calls) == 3


def test_job_ui_http_and_websocket_preserve_transport(native):
    connected()
    with client() as api:
        response = api.post("/api/v1/jobs/j/ui-proxy/8080/echo", content=b"hello", headers={"Range": "bytes=2-5"})
        assert response.status_code == 206 and response.content == b"hello"
        assert len(response.headers.get_list("set-cookie")) == 2
        with pytest.raises(WebSocketDisconnect):
            with api.websocket_connect(
                "/api/v1/jobs/j/ui-proxy/8080/ws/chat", headers={"Authorization": "Bearer remote-secret"}
            ):
                pass
        with api.websocket_connect("/api/v1/jobs/j/ui-proxy/8080/ws/chat", subprotocols=["chat"]) as ws:
            assert ws.accepted_subprotocol == "chat"
            ws.send_text("hello")
            assert ws.receive_text() == "hello"
            ws.send_bytes(b"frame")
            assert ws.receive_bytes() == b"frame"
            ws.send_text("close-worker")
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 4001 and closed.value.reason == "worker finished"
    assert native.closed.count("ui") == 2


def test_foreground_entrypoint_starts_only_uvicorn(native):
    script = """
import importlib.abc, subprocess, sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self,name,*args):
        if name.split('.')[0] in {'mn_runtime','redis','docker'}: raise AssertionError(name)
sys.meta_path.insert(0,NoRuntime())
def forbidden(*args,**kwargs): raise AssertionError("host process")
class ForbiddenProcess(subprocess.Popen):
    __init__=forbidden
subprocess.run=forbidden
subprocess.Popen=ForbiddenProcess
import mn_api.main as main
calls=[]
main.uvicorn.run=lambda *a,**kw: calls.append((a,kw))
main.start()
assert len(calls)==1 and calls[0][0]==("mn_api.main:app",)
assert calls[0][1]["host"] in {"localhost","127.0.0.1"} and calls[0][1]["port"]==54001
"""
    env = {k: v for k, v in os.environ.items() if k not in {"MN_API_HOST", "MN_API_PORT"}}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
