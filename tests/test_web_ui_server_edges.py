from __future__ import annotations

import urllib.error
from email.message import Message

from fastapi.testclient import TestClient

from mn_api.web_ui_server import _proxy_headers, _response_headers, _stream_response, _target_url, create_app


def test_proxy_request_returns_upstream_http_errors(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    headers = Message()
    headers["Content-Type"] = "application/json"

    def raise_http_error(request, timeout=30):
        raise urllib.error.HTTPError(request.full_url, 418, "teapot", headers, None)

    monkeypatch.setattr("mn_api.web_ui_server.urllib.request.urlopen", raise_http_error)

    response = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1")).get("/api/v1/jobs")

    assert response.status_code == 418
    assert response.headers["content-type"] == "application/json"


def test_proxy_request_returns_safe_gateway_error(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(
        "mn_api.web_ui_server.urllib.request.urlopen",
        lambda request, timeout=30: (_ for _ in ()).throw(RuntimeError("upstream down")),
    )

    response = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1")).get("/api/v1/jobs?limit=1")

    assert response.status_code == 502
    assert response.json()["component"] == "web-ui-proxy"
    assert response.json()["target"] == "http://api.local/api/v1/jobs?limit=1"


def test_proxy_header_and_target_helpers_filter_hop_by_hop_headers():
    assert _target_url("runs/run:1", "a=b", "http://api.local/api/v1") == "http://api.local/api/runs/run:1?a=b"
    assert _proxy_headers(
        [("Host", "local"), ("Connection", "close"), ("X-Test", "yes")],
        api_token="secret",
    ) == {"X-Test": "yes", "Authorization": "Bearer secret"}
    assert _proxy_headers([("authorization", "Bearer user")], api_token="secret") == {"authorization": "Bearer user"}
    assert _response_headers([("content-length", "1"), ("server", "uvicorn"), ("x-test", "yes")]) == {"x-test": "yes"}


def test_stream_response_closes_upstream_response():
    class Upstream:
        def __init__(self):
            self.lines = [b": heartbeat\n", b"\n", b"data: {}\n", b"\n", b""]
            self.closed = False

        def readline(self):
            return self.lines.pop(0)

        def read(self, _size):
            raise AssertionError("SSE proxy must not buffer fixed-size blocks")

        def close(self):
            self.closed = True

    upstream = Upstream()

    assert list(_stream_response(upstream)) == [b": heartbeat\n", b"\n", b"data: {}\n", b"\n"]
    assert upstream.closed is True
