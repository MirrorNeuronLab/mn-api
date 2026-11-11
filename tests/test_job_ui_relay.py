"""Surface URLs, path policy, and native UI protocol adaptation."""

from email.message import Message
from io import BytesIO
import pytest
from fastapi import HTTPException
from mn_api import job_ui_routing as routing, web_ui_server as proxy


def handle():
    return {
        "web_ui": {
            "url": "http://private-docker:41371/",
            "status": "running",
            "metadata": {"readiness": {"ready": True}, "proxy": {"http_ports": [41371], "websocket_ports": [41371]}},
        }
    }


def test_local_proxy_forwards_to_owner_api_with_only_server_credentials(monkeypatch):
    value = handle()["web_ui"]
    value["transport"] = {
        "schema_version": "mn.web_ui.transport.v1",
        "owner_api_url": "http://10.0.4.32:54001/api/v1",
        "execution_id": "execution",
    }
    monkeypatch.setattr(proxy, "_load_job_web_ui", lambda *a, **k: value)
    seen = []

    class Upstream(BytesIO):
        status = 200
        headers = Message()

        def getcode(self):
            return self.status

    def open_request(request, **kwargs):
        seen.append(request)
        return Upstream(b"code")

    monkeypatch.setattr(proxy, "_open_job_ui", open_request)
    response = proxy.proxy_job_ui_request(
        job_id="job",
        port=41371,
        path="app.js",
        query="v=2",
        method="GET",
        request_headers=[("Authorization", "Bearer browser"), ("Cookie", "secret=1")],
        upstream_api="http://local/api/v1",
        api_token="server-token",
    )
    assert response.status_code == 200
    assert seen[0].full_url == "http://10.0.4.32:54001/api/v1/jobs/job/ui-proxy/41371/app.js?v=2"
    assert seen[0].get_header("Authorization") == "Bearer server-token"
    assert seen[0].get_header("X-mn-ui-execution") == "execution"
    assert seen[0].get_header("Cookie") is None


def test_websocket_close_tolerates_a_peer_that_has_already_disconnected():
    import asyncio
    from fastapi import WebSocketDisconnect

    class Disconnected:
        async def close(self, **kwargs):
            raise WebSocketDisconnect(1006)

    asyncio.run(proxy._close_websocket(Disconnected(), 1000))


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "bad"},
        {"execution_id": ""},
        {"owner_api_url": "http://user:secret@host/api/v1"},
        {"owner_api_url": "http://host/elsewhere"},
    ],
)
def test_invalid_surface_transport_is_rejected(change):
    transport = {
        "schema_version": "mn.web_ui.transport.v1",
        "execution_id": "run",
        "owner_api_url": "https://surface.example/api/v1",
        **change,
    }
    with pytest.raises(HTTPException):
        routing.relay_target({"transport": transport}, "job", 41371, "", "", websocket=False)


def test_surface_relay_urls_bind_job_and_validate_path():
    value = {
        "transport": {
            "schema_version": "mn.web_ui.transport.v1",
            "execution_id": "run",
            "owner_api_url": "https://surface.example/api/v1",
        }
    }
    assert (
        routing.relay_target(value, "job id", 41371, "assets/app.js", "v=2")
        == "https://surface.example/api/v1/jobs/job%20id/ui-proxy/41371/assets/app.js?v=2"
    )
    assert routing.relay_target(value, "job", 41371, "bridge", "", websocket=True).startswith("wss://surface.example/")
    with pytest.raises(HTTPException):
        routing.relay_target(value, "job", 41371, "../secret", "", websocket=False)
