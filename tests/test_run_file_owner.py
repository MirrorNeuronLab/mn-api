"""A completed remote run must be readable before its ingress replica arrives."""

import io
import json
import asyncio
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mn_api import state
from mn_api.app import create_app
from mn_api.contracts import API_CONTRACT
from mn_api.routes.v1 import jobs
from mn_api.run_file_routing import BINDING_HEADER, HOP_HEADER, _binding, owner_file_response
from starlette.requests import Request
from starlette.requests import ClientDisconnect


class Upstream(io.BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json", "X-API-Contract": API_CONTRACT, **(headers or {})}


@pytest.fixture
def remote_run(monkeypatch, tmp_path):
    monkeypatch.setenv("MN_API_TOKEN", "")
    monkeypatch.setenv("MN_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("MN_HOST_SHARED_STORAGE_ROOT", str(tmp_path / "shared"))
    config = SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[])
    monkeypatch.setattr(state, "config", config)
    monkeypatch.setattr(state, "refresh_config_from_env", lambda: config)
    run = {"run_id": "run-1", "job_id": "job-1", "status": "completed",
           "owner_node": "owner@node", "run_data_ref": {
               "storage": "syncthing", "submission_id": "definition", "run_id": "physical-1"}}
    node = {"name": "owner@node", "address": "10.0.4.32", "self?": False,
            "hardware": {"http_api": {"schema_version": "mn.http_api.endpoint.v1", "enabled": True,
                                      "scheme": "http", "port": 54007}}}
    reads = []
    runtime = SimpleNamespace(get_run=lambda run_id: reads.append(run_id) or run)
    monkeypatch.setattr(jobs, "_service", lambda: runtime)
    monkeypatch.setattr(state, "client", SimpleNamespace(get_system_summary=lambda: json.dumps({"nodes": [node]})))
    calls = []
    upstreams = []

    def respond(body, headers=None, status=200):
        upstream = Upstream(body, headers)
        upstream.status = status
        upstreams.append(upstream)
        class Opener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                upstream.headers.setdefault("X-MN-Run-File-Binding", request.get_header("X-mn-run-file-binding"))
                return upstream
        monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: Opener())
        return upstream

    return SimpleNamespace(client=TestClient(create_app()), run=run, node=node, reads=reads,
                           calls=calls, respond=respond, config=config, upstreams=upstreams,
                           directory=tmp_path / "shared/submissions/definition/outputs/runs/physical-1")


@pytest.mark.parametrize("suffix,body", [
    ("artifacts/final", b'{"run_id":"run-1","status":"review_ready"}'),
    ("artifacts", b'{"items":[],"next_page_token":null}'),
    ("artifacts/report.md", b"Report bytes\n"),
    ("outputs", b'{"items":[{"name":"report.md"}],"next_page_token":null}'),
    ("outputs/0", b"Verified report\n"),
])
def test_remote_run_files_use_owner_before_local_replication(remote_run, suffix, body):
    remote = remote_run
    upstream = remote.respond(body)
    response = remote.client.get(f"/api/v1/runs/run-1/{suffix}")
    assert response.status_code == 200, response.text
    assert response.content == body
    assert remote.reads == ["run-1"]
    assert len(remote.calls) == 1
    request, timeout = remote.calls[0]
    assert request.full_url == f"http://10.0.4.32:54007/api/v1/runs/run-1/{suffix}"
    assert request.get_header("X-mn-run-file-hop") == "1"
    assert len(request.get_header("X-mn-run-file-binding")) == 64
    assert 0 < timeout <= 15
    assert upstream.closed


def test_owner_authentication_pagination_and_byte_range_are_preserved(remote_run):
    remote = remote_run
    remote.config.api_token = "configured-token"
    remote.respond(b"partial", {"Content-Range": "bytes 1-7/20", "Content-Type": "text/plain",
                               "Set-Cookie": "private-cookie", "X-Internal": "private"}, status=206)
    response = remote.client.get("/api/v1/runs/run-1/outputs/0?download=true", headers={
        "Authorization": "Bearer configured-token", "Range": "bytes=1-7", "If-Range": '"hash"',
        "Cookie": "private-caller-cookie",
    })
    assert response.status_code == 206
    assert response.content == b"partial"
    assert response.headers["content-range"] == "bytes 1-7/20"
    assert "set-cookie" not in response.headers and "x-internal" not in response.headers
    request = remote.calls[0][0]
    assert request.full_url.endswith("outputs/0?download=true")
    assert request.get_header("Authorization") == "Bearer configured-token"
    assert request.get_header("Range") == "bytes=1-7"
    assert request.get_header("If-range") == '"hash"'
    assert request.get_header("Accept-encoding") == "identity"
    assert request.get_header("Cookie") is None


def test_authentication_precedes_any_owner_or_file_read(remote_run):
    remote_run.config.api_token = "configured-token"
    response = remote_run.client.get("/api/v1/runs/run-1/outputs")
    assert response.status_code == 401
    assert remote_run.reads == remote_run.calls == []


def test_queued_admission_owns_the_same_canonical_file_binding(remote_run):
    remote = remote_run
    remote.run["admission"] = {"owner_node": remote.run.pop("owner_node")}
    remote.respond(b'{"items":[]}')
    assert remote.client.get("/api/v1/runs/run-1/outputs").status_code == 200
    assert len(remote.calls) == 1


@pytest.mark.parametrize("kind", ["local", "legacy", "http_disabled", "no_owner"])
def test_local_and_legacy_owners_keep_verified_replica_reads(remote_run, kind):
    remote = remote_run
    if kind == "local":
        remote.node["self?"] = True
    elif kind == "legacy":
        remote.node["hardware"].pop("http_api")
    elif kind == "http_disabled":
        remote.node["hardware"]["http_api"]["enabled"] = False
    else:
        remote.run.pop("owner_node")
    remote.directory.mkdir(parents=True)
    (remote.directory / "report.md").write_bytes(b"verified local bytes")
    response = remote.client.get("/api/v1/runs/run-1/artifacts/report.md")
    assert response.status_code == 200, response.text
    assert response.content == b"verified local bytes"
    assert remote.calls == []
    assert remote.reads == ["run-1"]


def test_owner_confirms_binding_on_an_actual_local_file_response(remote_run):
    remote = remote_run
    remote.node["self?"] = True
    remote.directory.mkdir(parents=True)
    (remote.directory / "report.md").write_bytes(b"bound report")
    binding = _binding(remote.run, "owner@node")
    response = remote.client.get("/api/v1/runs/run-1/artifacts/report.md", headers={
        HOP_HEADER: "1", BINDING_HEADER: binding,
    })
    assert response.status_code == 200
    assert response.content == b"bound report"
    assert response.headers[BINDING_HEADER] == binding
    assert remote.calls == []


@pytest.mark.parametrize("change", ["binding", "remote_owner", "missing_owner", "invalid_hop"])
def test_one_hop_and_owner_binding_fail_closed(remote_run, change):
    remote = remote_run
    remote.node["self?"] = change != "remote_owner"
    binding = _binding(remote.run, "owner@node")
    headers = {HOP_HEADER: "1", BINDING_HEADER: binding}
    if change == "binding":
        headers[BINDING_HEADER] = "0" * 64
    elif change == "missing_owner":
        remote.run.pop("owner_node")
    elif change == "invalid_hop":
        headers[HOP_HEADER] = "2"
    response = remote.client.get("/api/v1/runs/run-1/outputs", headers=headers)
    assert response.status_code == (400 if change == "invalid_hop" else 409)
    assert BINDING_HEADER not in response.headers
    assert remote.calls == []


@pytest.mark.parametrize("change,status", [
    ("run_identity", 502), ("owner_missing", 503), ("bad_endpoint", 503), ("invalid_binding", 503),
])
def test_invalid_core_authority_cannot_select_an_arbitrary_peer(remote_run, change, status):
    remote = remote_run
    if change == "run_identity":
        remote.run["run_id"] = "another-run"
    elif change == "owner_missing":
        remote.node["name"] = "unrelated@node"
    elif change == "bad_endpoint":
        remote.node["address"] = "user@attacker.invalid/path"
    else:
        remote.run["run_data_ref"] = {"invalid": float("nan")}
    response = remote.client.get("/api/v1/runs/run-1/outputs")
    assert response.status_code == status
    assert remote.calls == []


@pytest.mark.parametrize("change", ["status", "contract", "binding", "encoding"])
def test_unconfirmed_upstream_response_is_closed_and_rejected(remote_run, change):
    remote = remote_run
    headers = {}
    if change == "contract":
        headers["X-API-Contract"] = "another-service"
    elif change == "binding":
        headers[BINDING_HEADER] = "unconfirmed"
    elif change == "encoding":
        headers["Content-Encoding"] = "gzip"
    upstream = remote.respond(b"private payload", headers, status=201 if change == "status" else 200)
    response = remote.client.get("/api/v1/runs/run-1/outputs")
    assert response.status_code == 502
    assert "private payload" not in response.text
    assert upstream.closed


@pytest.mark.parametrize("code,expected", [(301, 503), (302, 503), (400, 400), (422, 422), (401, 503), (403, 503),
                                          (404, 404), (409, 409), (416, 416), (500, 503), (503, 503)])
def test_peer_errors_never_redirect_credentials_or_substitute_local_data(remote_run, monkeypatch, code, expected):
    remote = remote_run
    payload = io.BytesIO(b"private upstream diagnostic")
    handlers = []

    class Opener:
        def open(self, request, timeout):
            remote.calls.append((request, timeout))
            raise urllib.error.HTTPError(request.full_url, code, "private diagnostic",
                                         {"Location": "https://attacker.invalid/"}, payload)

    def opener(*configured):
        handlers.extend(configured)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", opener)
    response = remote.client.get("/api/v1/runs/run-1/outputs")
    assert response.status_code == expected
    assert "private" not in response.text and "attacker.invalid" not in response.text
    assert len(remote.calls) == 1
    assert payload.closed
    redirect = next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid") is None


@pytest.mark.parametrize("error", [TimeoutError("private"), OSError("private"),
                                 ValueError("private"), urllib.error.URLError("private")])
def test_bounded_transport_failures_are_sanitized(remote_run, monkeypatch, error):
    class Opener:
        def open(self, _request, timeout):
            assert timeout == 15
            raise error

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    response = remote_run.client.get("/api/v1/runs/run-1/outputs")
    assert response.status_code == 503
    assert "private" not in response.text


def test_abandoned_stream_closes_upstream_even_before_iteration(remote_run):
    remote = remote_run
    upstream = remote.respond(b"bytes")
    request = Request({"type": "http", "method": "GET", "path": "/api/v1/runs/run-1/outputs/0",
                       "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80)})
    response = owner_file_response("run-1", remote.run, request, "outputs/0")
    assert not upstream.closed
    asyncio.run(response.background())
    assert upstream.closed


@pytest.mark.parametrize("summary", [None, "invalid json", {}, {"nodes": None}, {"nodes": []}])
def test_invalid_membership_never_selects_a_peer(remote_run, monkeypatch, summary):
    monkeypatch.setattr(state.client, "get_system_summary", lambda: summary)
    response = remote_run.client.get("/api/v1/runs/run-1/outputs")
    assert response.status_code == 503
    assert remote_run.calls == []


@pytest.mark.parametrize("fenced", [False, True])
def test_documented_offline_replica_read_cannot_acknowledge_an_unverified_owner(remote_run, monkeypatch, tmp_path, fenced):
    def unavailable(_run_id):
        raise OSError("Core unavailable")
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(get_run=unavailable))
    monkeypatch.setattr(jobs, "stored_run", lambda _id: {"run_id": "run-1"})
    directory = tmp_path / "runs/run-1"
    directory.mkdir(parents=True)
    (directory / "report.md").write_bytes(b"saved local report")
    headers = {HOP_HEADER: "1", BINDING_HEADER: "0" * 64} if fenced else {}
    response = remote_run.client.get("/api/v1/runs/run-1/artifacts/report.md", headers=headers)
    assert response.status_code == (503 if fenced else 200)
    if not fenced:
        assert response.content == b"saved local report"
    assert BINDING_HEADER not in response.headers
    assert remote_run.calls == []


def test_interrupted_stream_closes_the_owner_response(remote_run, monkeypatch):
    upstream = remote_run.respond(b"partial")
    def broken(_size):
        raise OSError("disconnected upstream")
    monkeypatch.setattr(upstream, "read1", broken)
    with pytest.raises(OSError, match="disconnected upstream"):
        remote_run.client.get("/api/v1/runs/run-1/outputs/0")
    assert upstream.closed


def test_owner_fences_json_metadata_as_well_as_file_bytes(remote_run):
    remote = remote_run
    remote.node["self?"] = True
    remote.directory.mkdir(parents=True)
    (remote.directory / "final_artifact.json").write_text('{"status":"review_ready"}')
    binding = _binding(remote.run, "owner@node")
    response = remote.client.get("/api/v1/runs/run-1/artifacts/final", headers={
        HOP_HEADER: "1", BINDING_HEADER: binding,
    })
    assert response.status_code == 200
    assert response.json()["run_id"] == "run-1"
    assert response.headers[BINDING_HEADER] == binding
    assert remote.reads == ["run-1"]


@pytest.mark.parametrize("fail_on", ["http.response.start", "http.response.body"])
def test_client_disconnect_closes_upstream_before_or_during_body(remote_run, fail_on):
    remote = remote_run
    upstream = remote.respond(b"body bytes")
    scope = {"type": "http", "method": "GET", "path": "/api/v1/runs/run-1/outputs/0",
             "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80),
             "asgi": {"spec_version": "2.4"}}
    response = owner_file_response("run-1", remote.run, Request(scope), "outputs/0")
    async def send(message):
        if message["type"] == fail_on:
            raise OSError("client disconnected")
    async def receive():
        return {"type": "http.disconnect"}
    with pytest.raises(ClientDisconnect):
        asyncio.run(response(scope, receive, send))
    assert upstream.closed


def test_offline_missing_final_artifact_does_not_repeat_the_failed_core_read(remote_run, monkeypatch, tmp_path):
    reads = []
    def unavailable(run_id):
        reads.append(run_id)
        raise OSError("Core unavailable")
    monkeypatch.setattr(jobs, "_service", lambda: SimpleNamespace(get_run=unavailable))
    monkeypatch.setattr(jobs, "stored_run", lambda _id: {"run_id": "run-1"})
    (tmp_path / "runs/run-1").mkdir(parents=True)
    response = remote_run.client.get("/api/v1/runs/run-1/artifacts/final")
    assert response.status_code == 404
    assert reads == ["run-1"]
    assert remote_run.calls == []
