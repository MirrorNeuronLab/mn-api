"""Owner routing never resolves a peer's private Docker hostname locally."""

import json
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mn_api import state, job_ui_routing as routing, web_ui_server as proxy
from mn_api.routes.v1 import job_ui_relay as relay


def handle():
    return {"job_id": "job", "web_ui": {
        "url": "http://private-docker:41371/?control=1", "status": "running",
        "metadata": {"execution_id": "execution", "readiness": {"ready": True},
                     "proxy": {"http_ports": [41371], "websocket_ports": [41371]}},
    }}


def core(monkeypatch, *, local=False, api_port=54001):
    monkeypatch.setattr(state, "client", SimpleNamespace(
        get_job=lambda _: json.dumps({"owner_node": "core@spark", "latest_run_id": "execution"}),
        get_system_summary=lambda: json.dumps({"nodes": [{"name": "core@spark", "address": "10.0.4.32", "self?": local,
            "hardware": {"http_api": {"schema_version": "mn.http_api.endpoint.v1", "scheme": "http", "enabled": True, "port": api_port}}}]}),
    ))


def test_routing_uses_core_owner_and_binds_execution(monkeypatch, state_snapshot):
    core(monkeypatch, api_port=54007)
    monkeypatch.setattr(state, "config", SimpleNamespace(port=61407))
    value = routing.routed_handle(handle())
    transport = value["web_ui"]["transport"]
    assert transport["owner_api_url"] == "http://10.0.4.32:54007/api/v1"
    assert transport["execution_id"] == "execution"
    target = routing.relay_target(value["web_ui"], "job", 41371, "assets/app.js", "v=2")
    assert target.endswith("/api/v1/jobs/job/ui-proxy/41371/assets/app.js?v=2")
    assert "private-docker" not in target
    assert routing.relay_target(value["web_ui"], "job", 41371, "bridge", "control=1", websocket=True).endswith("/41371/ws/bridge?control=1")
    assert routing.relay_target(value["web_ui"], "job", 41371, "", "", websocket=True).startswith("ws://")
    value["web_ui"]["metadata"]["execution_id"] = "earlier"
    assert routing.routed_handle(value)["web_ui"]["status"] == "stopped"


def test_idle_registration_cannot_supply_transport_authority(monkeypatch, state_snapshot):
    monkeypatch.setattr(state, "client", SimpleNamespace(get_job=lambda _: "{}"))
    value = handle()
    value["web_ui"]["transport"] = {"owner_api_url": "http://untrusted/api/v1"}
    assert "transport" not in routing.routed_handle(value)["web_ui"]


@pytest.mark.parametrize("changed", ["execution", "hop", "owner", "port", "lifecycle", "registration"])
def test_owner_relay_rejects_stale_loops_undeclared_ports_and_inactive_services(monkeypatch, state_snapshot, changed):
    core(monkeypatch, local=changed != "owner")
    value = handle()
    headers = {"x-mn-ui-hop": "1", "x-mn-ui-execution": "execution", "x-mn-job-ui-probe": "1"}
    if changed == "execution":
        headers["x-mn-ui-execution"] = "earlier"
    if changed == "hop":
        headers["x-mn-ui-hop"] = "2"
    if changed == "lifecycle":
        value["web_ui"]["status"] = "paused"
    if changed == "registration":
        value["web_ui"]["metadata"]["execution_id"] = "earlier"
    monkeypatch.setattr(relay.jobs, "get_job_ui", lambda *a, **k: value)
    with pytest.raises(HTTPException):
        relay.local_target("job", 9999 if changed == "port" else 41371, "", "", headers)


def test_owner_relay_streams_and_strips_credentials_before_worker(monkeypatch, state_snapshot, tmp_path):
    core(monkeypatch, local=True)
    monkeypatch.setattr(relay.jobs, "get_job_ui", lambda *a, **k: handle())
    monkeypatch.setattr(relay.jobs, "probe_job_web_ui", lambda *a: {"ready": True})
    monkeypatch.setattr(relay.RuntimeConfig, "from_env", lambda: SimpleNamespace(web_ui_url="http://localhost:55173"))
    monkeypatch.setattr(relay, "WebUiConfig", SimpleNamespace(from_env=lambda: SimpleNamespace(api_token="owner-token", proxy_timeout_seconds=3)))
    seen = []

    class Upstream(BytesIO):
        status = 200
        headers = Message()
    upstream = Upstream(b"first frame\nsecond frame")
    upstream.headers["Content-Type"] = "multipart/x-mixed-replace;boundary=x"
    def open_request(request, **kwargs):
        seen.append(request)
        return upstream
    monkeypatch.setattr(relay.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=open_request))
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(relay.router)
    app.dependency_overrides[relay.require_auth] = lambda: "authenticated"
    response = TestClient(app).get("/api/v1/jobs/job/ui-proxy/41371/stream?control=1", headers={
        "X-MN-UI-Hop": "1", "X-MN-UI-Execution": "execution", "Authorization": "Bearer caller", "Cookie": "private=1"})
    assert response.content == b"first frame\nsecond frame" and upstream.closed
    assert seen[0].full_url.endswith("/job-ui-proxy/job/41371/stream?control=1")
    assert seen[0].get_header("Authorization") == "Bearer owner-token"
    assert seen[0].get_header("Cookie") is None
    assert seen[0].get_header("X-mn-ui-hop") is None


def test_ordinary_owner_requests_require_fresh_page_readiness(monkeypatch, state_snapshot):
    core(monkeypatch, local=True)
    monkeypatch.setattr(relay.jobs, "get_job_ui", lambda *a, **k: handle())
    monkeypatch.setattr(relay.jobs, "probe_job_web_ui", lambda *a: {"ready": False, "reason": "assets_unavailable"})
    with pytest.raises(HTTPException, match="not running"):
        relay.local_target("job", 41371, "", "", {"x-mn-ui-hop": "1", "x-mn-ui-execution": "execution"})


def test_local_proxy_forwards_to_owner_api_with_only_server_credentials(monkeypatch):
    value = handle()["web_ui"]
    value["transport"] = {"schema_version": "mn.web_ui.transport.v1", "owner_api_url": "http://10.0.4.32:54001/api/v1", "execution_id": "execution"}
    monkeypatch.setattr(proxy, "_load_job_web_ui", lambda *a, **k: value)
    seen = []
    class Upstream(BytesIO):
        status = 200
        headers = Message()
        def getcode(self): return self.status
    def open_request(request, **kwargs):
        seen.append(request)
        return Upstream(b"code")
    monkeypatch.setattr(proxy, "_open_job_ui", open_request)
    response = proxy.proxy_job_ui_request(job_id="job", port=41371, path="app.js", query="v=2", method="GET",
        request_headers=[("Authorization", "Bearer browser"), ("Cookie", "secret=1")], upstream_api="http://local/api/v1", api_token="server-token")
    assert response.status_code == 200
    assert seen[0].full_url == "http://10.0.4.32:54001/api/v1/jobs/job/ui-proxy/41371/app.js?v=2"
    assert seen[0].get_header("Authorization") == "Bearer server-token"
    assert seen[0].get_header("X-mn-ui-execution") == "execution"
    assert seen[0].get_header("Cookie") is None


def test_owner_websocket_authentication_frames_and_disconnect(monkeypatch, state_snapshot):
    import asyncio
    from contextlib import asynccontextmanager
    import websockets

    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="api-token"))
    monkeypatch.setattr(relay, "WebUiConfig", SimpleNamespace(from_env=lambda: SimpleNamespace(api_token="owner-token", proxy_timeout_seconds=3)))
    monkeypatch.setattr(relay, "local_target", lambda *a, **k: "ws://localhost:55173/job-ui-proxy/job/41371/ws/bridge")

    async def scenario():
        received = asyncio.Event()
        seen, sent, closed = [], [], []
        class Browser:
            headers = {"authorization": "Bearer api-token", "sec-websocket-key": "browser-key"}
            url = SimpleNamespace(query="control=1")
            count = 0
            async def accept(self):
                seen.append("accepted")
            async def close(self, code):
                seen.append(code)
            async def receive(self):
                self.count += 1
                if self.count == 1:
                    return {"type": "websocket.receive", "text": "command"}
                if self.count == 2:
                    return {"type": "websocket.receive", "bytes": b"command"}
                await received.wait()
                return {"type": "websocket.disconnect"}
            async def send_text(self, value):
                sent.append(value)
            async def send_bytes(self, value):
                sent.append(value)
                received.set()
        class Upstream:
            async def send(self, value):
                seen.append(value)
            async def __aiter__(self):
                yield "state"
                yield b"state"
                await asyncio.Event().wait()
        @asynccontextmanager
        async def connect(target, **options):
            assert target.startswith("ws://localhost:")
            assert options["additional_headers"] == {"X-MN-Job-UI-Probe": "1", "Authorization": "Bearer owner-token"}
            try:
                yield Upstream()
            finally:
                closed.append(True)
        monkeypatch.setattr(websockets, "connect", connect)
        await asyncio.wait_for(relay.relay_websocket(Browser(), "job", 41371, "bridge"), 2)
        assert sent == ["state", b"state"]
        assert "command" in seen and b"command" in seen
        assert closed == [True]
        browser = Browser()
        browser.headers = {"authorization": "Bearer wrong"}
        await relay.relay_websocket(browser, "job", 41371, "bridge")
        assert seen[-1] == 1008
    asyncio.run(scenario())


def test_websocket_close_tolerates_a_peer_that_has_already_disconnected():
    import asyncio
    from fastapi import WebSocketDisconnect
    class Disconnected:
        async def close(self, **kwargs):
            raise WebSocketDisconnect(1006)
    asyncio.run(proxy._close_websocket(Disconnected(), 1000))
