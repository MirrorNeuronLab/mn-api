import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mn_api.client_app import create_client_app
from mn_sdk.client_install import RemoteConnection, save_connection


class Stream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MN_HOME", str(tmp_path))
    monkeypatch.delenv("MN_API_TOKEN", raising=False)
    (tmp_path / "install_metadata.json").write_text('{"install_role":"client"}')
    (tmp_path / "api.token").write_text("local-secret")
    return tmp_path


def connected():
    save_connection(RemoteConnection("https://remote/api/v1", "remote-secret", "node@remote"))


def client(**kwargs):
    return TestClient(create_client_app(**kwargs), headers={"Authorization": "Bearer local-secret"})


def test_health_and_connections_are_local_and_private(home):
    forwarded = []

    def handshake(request):
        forwarded.append(request)
        if request.headers["authorization"] != "Bearer remote-secret":
            return httpx.Response(401)
        payload = (
            {"api_contract": "mirrorneuron.rest.v1"}
            if request.url.path.endswith("/health")
            else {"identity": {"actual": "node@remote", "valid": True}}
        )
        return httpx.Response(200, json=payload)

    with client(connection_transport=httpx.MockTransport(handshake)) as api:
        assert api.get("/api/v1/health", headers={"Authorization": ""}).json() == {
            "status": "ok",
            "api_contract": "mirrorneuron.rest.v1",
            "auth": "enabled",
        }
        assert not forwarded
        assert api.get("/api/v1/jobs", headers={"Authorization": "Bearer remote-secret"}).status_code == 401
        assert api.get("/api/v1/jobs").status_code == 503
        added = api.post("/api/v1/nodes", json={"host": "https://remote", "token": "remote-secret"})
        assert added.status_code == 201
        assert "remote-secret" not in added.text
        assert len(forwarded) == 2
        before = (home / "client.json").read_bytes()
        assert api.post("/api/v1/nodes", json={"host": "https://bad", "token": "bad"}).status_code == 401
        assert (home / "client.json").read_bytes() == before
        assert api.delete("/api/v1/nodes/wrong").status_code == 409
        assert api.delete("/api/v1/nodes/node@remote").json()["status"] == "disconnected"
        assert not (home / "client.json").exists()
        assert all(r.method == "GET" for r in forwarded)  # No federation POST/DELETE.


@pytest.mark.parametrize(
    "status,content_type,body",
    [
        (202, "application/json", b'{"run_id":"r1"}'),
        (401, "application/problem+json", b'{"code":"unauthorized"}'),
        (409, "application/problem+json", b'{"code":"conflict"}'),
        (206, "application/octet-stream", b"\x00output\xff"),
        (200, "text/event-stream", b'id: 1\ndata: {"status":"running"}\n\n'),
    ],
)
def test_relay_preserves_contract_headers_body_and_status(home, status, content_type, body):
    connected()
    requests = []
    stream = Stream(body[:3], body[3:])

    async def remote(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer remote-secret"
        assert request.headers["idempotency-key"] == "submission-1"
        assert request.headers["if-match"] == '"rev-2"'
        assert request.headers["last-event-id"] == "1"
        assert await request.aread() == b'{"admission_policy":"enqueue_if_busy"}'
        return httpx.Response(
            status,
            stream=stream,
            headers={
                "Content-Type": content_type,
                "ETag": '"rev-3"',
                "Retry-After": "2",
                "Content-Disposition": 'attachment; filename="result.zip"',
                "Location": "https://remote/api/v1/runs/r1",
                "X-Request-ID": "remote-request",
            },
        )

    with client(transport=httpx.MockTransport(remote)) as api:
        response = api.post(
            "/api/v1/blueprints/bp/runs?node=a%40b",
            content=b'{"admission_policy":"enqueue_if_busy"}',
            headers={"Idempotency-Key": "submission-1", "If-Match": '"rev-2"', "Last-Event-ID": "1"},
        )
        assert response.status_code == status
        assert response.content == body
        assert response.headers["content-type"] == content_type
        assert response.headers["etag"] == '"rev-3"'
        assert response.headers["location"] == "/api/v1/runs/r1"
        assert response.headers["x-request-id"] == "remote-request"
        assert response.headers["retry-after"] == "2"
        assert response.headers["content-disposition"].endswith('"result.zip"')
    assert requests[0].url.query == b"node=a%40b"
    assert stream.closed


def test_live_reconnection_applies_to_next_request(home):
    connected()
    observed = []

    async def remote(request):
        observed.append((request.url.host, request.headers["authorization"]))
        return httpx.Response(200, stream=Stream(b'{"items":[]}'))

    with client(transport=httpx.MockTransport(remote)) as api:
        api.get("/api/v1/nodes")
        save_connection(RemoteConnection("https://new/api/v1", "new-token", "new-node"))
        api.get("/api/v1/nodes")
    assert observed == [("remote", "Bearer remote-secret"), ("new", "Bearer new-token")]


def test_remote_failure_is_sanitized_and_never_retried(home):
    connected()
    calls = []

    async def remote(request):
        calls.append(request)
        raise httpx.ConnectError("remote-secret failure", request=request)

    with client(transport=httpx.MockTransport(remote)) as api:
        result = api.post("/api/v1/blueprints/bp/runs", json={})
        assert result.status_code == 503
        assert "remote-secret" not in result.text
        assert "before retrying" in result.json()["hint"]
    assert len(calls) == 1


def test_websocket_auth_and_bidirectional_job_ui(home):
    connected()
    observed = []

    class Upstream:
        subprotocol = None

        def __init__(self):
            self.messages = asyncio.Queue()

        async def send(self, message):
            await self.messages.put(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

    @asynccontextmanager
    async def connector(url, **kwargs):
        observed.append((url, kwargs))
        yield Upstream()

    with client(websocket_connect=connector) as api:
        with pytest.raises(WebSocketDisconnect):
            with api.websocket_connect("/api/v1/jobs/j/ui/ws", headers={"Authorization": "Bearer remote-secret"}):
                pass
        assert not observed
        with api.websocket_connect("/api/v1/jobs/j/ui/ws?channel=screen") as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "hello"
            ws.send_bytes(b"frame")
            assert ws.receive_bytes() == b"frame"
    assert observed[0][0] == "wss://remote/api/v1/jobs/j/ui/ws?channel=screen"
    assert observed[0][1]["additional_headers"]["Authorization"] == "Bearer remote-secret"


def test_foreground_entrypoint_does_not_load_runtime_state(home):
    script = """
import sys, subprocess
def forbidden(*args, **kwargs): raise AssertionError("local subprocess")
subprocess.run = subprocess.Popen = forbidden
import mn_api.main as main
assert "mn_api.state" not in sys.modules
assert "mn_api.app" not in sys.modules
assert "mn_api.mcp_server" not in sys.modules
calls = []
main.uvicorn.run = lambda *args, **kwargs: calls.append((args, kwargs))
main.start()
assert len(calls) == 1
assert calls[0][1]["host"] == "127.0.0.1"
assert calls[0][1]["port"] == 54001
"""
    env = {k: v for k, v in os.environ.items() if k not in {"MN_API_HOST", "MN_API_PORT"}}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cors_preflight_does_not_require_or_bypass_local_auth(home, monkeypatch):
    monkeypatch.setenv("MN_API_CORS_ALLOW_ORIGINS", "https://desktop.example")
    with TestClient(create_client_app()) as api:
        headers = {
            "Origin": "https://desktop.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        }
        response = api.options("/api/v1/nodes", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://desktop.example"
        assert api.post("/api/v1/nodes", json={}).status_code == 401


def test_relay_catalog_run_progress_cancel_and_results(home):
    connected()
    expected = [
        ("GET", "/blueprints", b'{"items":[{"blueprint_id":"bp"}]}'),
        ("POST", "/blueprints/bp/runs", b'{"run_id":"r","job_id":"j"}'),
        ("GET", "/runs/r/workflow-progress", b'{"status":"running"}'),
        ("PATCH", "/runs/r", b'{"status":"cancelled"}'),
        ("GET", "/runs/r/artifacts/final", b'{"answer":42}'),
        ("GET", "/runs/r/outputs/0", b"output data"),
    ]
    remaining = list(expected)

    async def remote(request):
        method, suffix, result = remaining.pop(0)
        assert request.method == method
        assert request.url.path == "/api/v1" + suffix
        assert request.headers["authorization"] == "Bearer remote-secret"
        if method == "PATCH":
            assert await request.aread() == b'{"desired_state":"cancelled"}'
        return httpx.Response(200, stream=Stream(result))

    with client(transport=httpx.MockTransport(remote)) as api:
        for method, suffix, payload in expected:
            response = api.request(
                method, "/api/v1" + suffix, content=b'{"desired_state":"cancelled"}' if method == "PATCH" else b""
            )
            assert response.status_code == 200
            assert response.content == payload
    assert not remaining


def test_relay_keeps_multiple_job_ui_cookies_separate(home):
    connected()

    async def remote(_request):
        return httpx.Response(
            200,
            stream=Stream(b"job ui"),
            headers=[
                ("Content-Type", "text/html"),
                ("Set-Cookie", "session=remote-session; HttpOnly; Path=/api/v1/jobs/j/ui"),
                ("Set-Cookie", "csrf=remote-csrf; Path=/api/v1/jobs/j/ui"),
            ],
        )

    with client(transport=httpx.MockTransport(remote)) as api:
        result = api.get("/api/v1/jobs/j/ui/proxy")
        assert result.status_code == 200
        assert len(result.headers.get_list("set-cookie")) == 2
        assert result.headers.get_list("set-cookie")[0].startswith("session=")
        assert result.headers.get_list("set-cookie")[1].startswith("csrf=")
