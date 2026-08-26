from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from mn_sdk.errors import AppError

from mn_api import state
from mn_api.app import create_app
from mn_api.routes import system as legacy_system


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(state, "client", object())
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]),
    )
    monkeypatch.setattr(legacy_system, "detect_lan_ip", lambda: "10.0.0.1")
    return TestClient(create_app())


def test_node_create_waits_on_shared_federation_orchestrator(monkeypatch):
    client = _client(monkeypatch)
    calls = []

    def join(local_client, **kwargs):
        calls.append((local_client, kwargs))
        return {
            "node_name": "mirror_neuron@node-b",
            "status": "federated",
            "peer": {"peer_auth_token": "must-not-escape"},
        }

    monkeypatch.setattr(legacy_system, "join_federated_node", join)
    response = client.post(
        "/api/v1/nodes",
        json={"host": "node-b", "token": "one-time-join-token", "grpc_port": 55051},
    )

    assert response.status_code == 201
    assert response.headers["location"] == "/api/v1/nodes/mirror_neuron@node-b"
    assert response.json()["status"] == "federated"
    assert "auth_token" not in response.text
    assert len(calls) == 1
    assert calls[0][1]["grpc_port"] == 55051


def test_node_create_returns_sanitized_503_when_reciprocal_readiness_fails(monkeypatch):
    client = _client(monkeypatch)

    def fail(*_args, **_kwargs):
        raise AppError(
            "MN_FEDERATION_UNAVAILABLE",
            "The peer relationship did not become ready on both Cores.",
            internal_message="remote credential secret-material",
            http_status=503,
        )

    monkeypatch.setattr(legacy_system, "join_federated_node", fail)
    response = client.post(
        "/api/v1/nodes",
        json={"host": "node-b", "token": "one-time-join-token"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "MN_FEDERATION_UNAVAILABLE"
    assert "secret-material" not in response.text
    assert "one-time-join-token" not in response.text
