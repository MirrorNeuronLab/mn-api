from __future__ import annotations

import urllib.error
from email.message import Message

import pytest
from fastapi.testclient import TestClient

from mn_api.web_ui_server import (
    JobUiProxyError,
    _job_ui_target_url,
    _proxy_headers,
    _response_headers,
    _stream_response,
    _target_url,
    create_app,
)


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
    assert _target_url("runs/run:1", "a=b", "http://api.local/api/v1") == "http://api.local/api/v1/runs/run:1?a=b"
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


def test_job_ui_proxy_uses_the_registered_spark_host_and_injects_local_proxy_config(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    handle = {
        "url": "http://10.0.4.26:8088/",
        "metadata": {
            "proxy": {
                "http_ports": [8080, 8088],
                "websocket_ports": [9090],
            }
        },
    }
    captured: dict[str, str] = {}
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"

    class Upstream:
        status = 200
        closed = False

        def __init__(self):
            self.headers = headers

        def getcode(self):
            return self.status

        def read(self):
            return b"<html><head></head><body>ROS dashboard</body></html>"

        def close(self):
            self.closed = True

    def open_remote(request, timeout=30):
        captured["url"] = request.full_url
        return Upstream()

    monkeypatch.setattr("mn_api.web_ui_server._load_job_web_ui", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr("mn_api.web_ui_server.urllib.request.urlopen", open_remote)

    response = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1")).get(
        "/job-ui-proxy/job-1/8088/"
    )

    assert response.status_code == 200
    assert captured["url"] == "http://10.0.4.26:8088/"
    assert 'window.__MN_JOB_UI_PROXY_BASE__="/job-ui-proxy/job-1"' in response.text


def test_job_ui_proxy_normalizes_video_topic_and_streams_mjpeg(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    handle = {
        "url": "http://10.0.4.26:8088/",
        "metadata": {"proxy": {"http_ports": [8080, 8088]}},
    }
    captured: dict[str, str] = {}
    headers = Message()
    headers["Content-Type"] = "multipart/x-mixed-replace;boundary=boundarydonotcross"

    class Upstream:
        status = 200

        def __init__(self):
            self.headers = headers
            self.chunks = [
                b"--boundarydonotcross\r\nContent-type: image/jpeg\r\n\r\nJFIF",
                b"\r\n--boundarydonotcross--\r\n",
                b"",
            ]
            self.closed = False

        def getcode(self):
            return self.status

        def read(self, _size):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

    def open_remote(request, timeout=30):
        captured["url"] = request.full_url
        return Upstream()

    monkeypatch.setattr("mn_api.web_ui_server._load_job_web_ui", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr("mn_api.web_ui_server.urllib.request.urlopen", open_remote)

    response = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1")).get(
        "/job-ui-proxy/job-1/8080/stream?topic=%2Fcamera%2Fcolor%2Fimage_raw&type=mjpeg"
    )

    assert response.status_code == 200
    assert captured["url"] == "http://10.0.4.26:8080/stream?topic=/camera/color/image_raw&type=mjpeg"
    assert response.headers["content-type"] == "multipart/x-mixed-replace;boundary=boundarydonotcross"
    assert b"Content-type: image/jpeg" in response.content
    assert b"JFIF" in response.content


def test_job_ui_proxy_rejects_ports_not_declared_by_the_job(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    handle = {
        "url": "http://10.0.4.26:8088/",
        "metadata": {"proxy": {"http_ports": [8088], "websocket_ports": [9090]}},
    }
    monkeypatch.setattr("mn_api.web_ui_server._load_job_web_ui", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(
        "mn_api.web_ui_server.urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("an undeclared port must not be contacted"),
    )

    response = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1")).get(
        "/job-ui-proxy/job-1/8080/stream"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "That job Web UI connection is not declared."}


def test_job_ui_target_allows_only_declared_http_and_websocket_ports():
    handle = {
        "url": "https://10.0.4.26:8088/dashboard",
        "metadata": {"proxy": {"http_ports": [8080, 8088], "websocket_ports": [9090]}},
    }

    assert _job_ui_target_url(handle, port=8080, path="stream", query="topic=/camera") == (
        "https://10.0.4.26:8080/stream?topic=/camera"
    )
    assert _job_ui_target_url(
        handle,
        port=8080,
        path="stream",
        query="topic=%2Fcamera%2Fcolor%2Fimage_raw&type=mjpeg&quality=80&empty=&tag=one&tag=two",
    ) == (
        "https://10.0.4.26:8080/stream?topic=/camera/color/image_raw&type=mjpeg&quality=80&empty=&tag=one&tag=two"
    )
    assert _job_ui_target_url(
        handle,
        port=9090,
        path="ws",
        query="topic=%2Fcamera%2Fcolor%2Fimage_raw&empty=",
        websocket=True,
    ) == "wss://10.0.4.26:9090/ws?topic=/camera/color/image_raw&empty="
    assert _job_ui_target_url(handle, port=9090, path="", query="", websocket=True) == "wss://10.0.4.26:9090/"
    with pytest.raises(JobUiProxyError, match="not declared"):
        _job_ui_target_url(handle, port=8090, path="mcp", query="")


@pytest.mark.parametrize("status", ["paused", "stopped", "cancelled", "failed"])
def test_job_ui_target_rejects_an_inactive_service(status):
    handle = {
        "url": "http://10.0.4.26:8088/",
        "status": status,
        "metadata": {"proxy": {"http_ports": [8088]}},
    }

    with pytest.raises(JobUiProxyError, match="not running"):
        _job_ui_target_url(handle, port=8088, path="", query="")
