from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from mn_api.web_ui_server import create_app


def test_health_reports_dist_dir(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=\"root\"></div>", encoding="utf-8")

    client = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1"))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "component": "web-ui",
        "api_base_url": "http://api.local/api/v1",
        "dist_dir": str(dist),
    }


def test_serves_static_files_and_spa_fallback(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

    client = TestClient(create_app(dist_dir=dist, api_url="http://api.local/api/v1"))

    assert client.get("/").text == "INDEX"
    assert client.get("/assets/app.js").text == "console.log('ok')"
    assert client.get("/jobs/some-job").text == "INDEX"
    assert client.head("/jobs/some-job").status_code == 200
    assert client.get("/%2E%2E/secret").status_code == 404


def test_proxy_forwards_api_requests(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    server = _ApiServer()
    server.start()
    try:
        client = TestClient(create_app(dist_dir=dist, api_url=server.base_url))

        response = client.post("/api/v1/health?verbose=true", json={"hello": "world"})

        assert response.status_code == 201
        assert response.json() == {"status": "ok", "path": "/api/v1/health", "query": "verbose=true"}
        assert server.last_body == b'{"hello":"world"}'
    finally:
        server.stop()


def test_proxy_injects_configured_api_token(tmp_path: Path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX", encoding="utf-8")
    server = _ApiServer()
    server.start()
    monkeypatch.setenv("MN_API_TOKEN", "local-api-token")
    try:
        client = TestClient(create_app(dist_dir=dist, api_url=server.base_url))

        response = client.post("/api/v1/jobs", json={})

        assert response.status_code == 201
        assert server.last_authorization == "Bearer local-api-token"
    finally:
        server.stop()


class _ApiServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.owner = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.last_body = b""
        self.last_authorization = ""

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/api/v1"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        self.server.owner.last_body = self.rfile.read(length)
        self.server.owner.last_authorization = self.headers.get("authorization", "")
        path, _separator, query = self.path.partition("?")
        payload = json.dumps({"status": "ok", "path": path, "query": query})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, *_args):
        return
