import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mn_sdk import HostHardwareProfile, get_registered_model, load_model_registry
from mn_api import model_installation, state
from mn_api.app import create_app
from mn_api.operations import get_operation
from mn_api.routes.v1 import infrastructure


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        state, "config", SimpleNamespace(api_token="", request_size_limit_bytes=1048576, cors_allow_origins=[])
    )
    return TestClient(create_app())


def test_model_collection_defaults_to_catalog_and_marks_default_chain(client, monkeypatch):
    monkeypatch.setattr("mn_sdk.model_service.installed_model_names", lambda: set())
    response = client.get("/api/v1/models")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 5
    assert next(item for item in items if item["id"] == "cosmos3-nano-reasoner:1.7")["source"] == "docker"
    assert {item["id"] for item in items if item["default"]} == {"nemotron-3.5-lightning:latest", "gemma4:e2b"}
    assert client.get("/api/v1/models?installed_only=true").json()["items"] == []


def test_default_installation_runs_background_work_and_replays_idempotently(client, monkeypatch):
    finished = Event()
    calls = []

    def install(model_id, options, progress):
        calls.append((model_id, options))
        finished.set()
        return {"status": "ready", "model": "gemma4:e2b"}

    monkeypatch.setattr(infrastructure, "install_model", install)
    headers = {"Idempotency-Key": "install-default"}
    response = client.put("/api/v1/models/default/installation", json={}, headers=headers)
    assert response.status_code == 202
    assert response.headers["location"].startswith("/api/v1/operations/op-local-")
    assert finished.wait(2)
    replay = client.put("/api/v1/models/default/installation", json={}, headers=headers)
    assert replay.status_code == 202
    assert replay.headers["idempotency-replayed"] == "true"
    assert calls == [("default", {"backend": "auto", "context_size": None, "force": False})]


@pytest.mark.parametrize("remote", [False, True])
def test_default_install_selects_feasible_cluster_model(monkeypatch, remote):
    resources = [
        {
            "name": "local",
            "status": "healthy",
            "scheduling_eligible": True,
            "devices": [{"kind": "gpu", "memory_total_mb": 16384, "memory_free_mb": 16384}],
        }
    ]
    systems = [{"name": "local", "self": True, "grpc_host": "127.0.0.1"}]
    if remote:
        resources.append(
            {
                "name": "spark",
                "status": "healthy",
                "scheduling_eligible": True,
                "devices": [{"kind": "gpu", "memory_total_mb": 131072, "memory_free_mb": 131072}],
            }
        )
        systems.append({"name": "spark", "grpc_host": "10.0.0.2"})
    monkeypatch.setattr(
        state,
        "client",
        SimpleNamespace(
            get_resource=lambda: json.dumps({"nodes": resources}),
            get_system_summary=lambda: json.dumps({"nodes": systems}),
        ),
    )
    monkeypatch.setattr(
        "mn_sdk.model_runtime.detect_host_hardware",
        lambda: HostHardwareProfile(
            "darwin", "arm64", total_memory_gb=16, unified_memory_gb=16, has_apple_silicon=True
        ),
    )
    calls = []

    def prepare(**kwargs):
        calls.append(kwargs)
        return {"install": {"status": "installed"}, "endpoint": {"model": kwargs["entry"]["id"]}}

    monkeypatch.setattr(model_installation, "install_runtime_cluster_model_for_api", prepare)
    monkeypatch.setattr(model_installation, "sync_runtime_model_gateways_for_api", lambda summary: summary["endpoints"])
    result = model_installation.install_model("default", {}, lambda **_kwargs: None)
    expected = "nemotron-3.5-lightning:latest" if remote else "gemma4:e2b"
    assert result["model"] == expected
    assert result["node"] == ("spark" if remote else "local")
    assert [call["entry"]["id"] for call in calls] == [expected]
    assert get_registered_model(expected) is not None
    assert not load_model_registry().get("default_model_id")


def test_default_install_without_core_uses_local_fallback(monkeypatch):
    monkeypatch.setattr(state, "client", SimpleNamespace())
    monkeypatch.setattr(
        "mn_sdk.model_runtime.detect_host_hardware",
        lambda: HostHardwareProfile(
            "darwin", "arm64", total_memory_gb=16, unified_memory_gb=16, has_apple_silicon=True
        ),
    )
    installed = []
    monkeypatch.setattr(
        model_installation, "install_runtime_model", lambda model, **options: installed.append(model) or {}
    )
    monkeypatch.setattr(model_installation, "sync_runtime_model_gateways_for_api", lambda summary: summary["endpoints"])
    result = model_installation.install_model("default", {}, lambda **_kwargs: None)
    assert result["model"] == "gemma4:e2b"
    assert installed == ["gemma4:e2b"]


def test_default_install_reuses_operator_provider_default(monkeypatch):
    from mn_sdk import add_registered_models, provider_registration, set_registered_default_model

    add_registered_models(
        [provider_registration("custom-chat", source_model="upstream", api_base="https://example.test/v1")]
    )
    set_registered_default_model("custom-chat")
    monkeypatch.setattr(model_installation, "sync_litellm_gateway", lambda **_options: {"status": "running"})
    result = model_installation.install_model("default", {}, lambda **_kwargs: None)
    assert result["model"] == "custom-chat"
    assert result["reused"] is True
    assert load_model_registry()["default_model_id"] == "custom-chat"
    with pytest.raises(ValueError, match="cannot be used with a provider"):
        model_installation.install_model("default", {"context_size": 4096}, lambda **_kwargs: None)


def test_model_installation_failure_is_sanitized_problem_in_operation(client, monkeypatch):
    failed = Event()

    def install(*args):
        failed.set()
        raise RuntimeError("private installation details")

    monkeypatch.setattr(infrastructure, "install_model", install)
    response = client.put("/api/v1/models/default/installation", json={})
    assert response.status_code == 202
    assert failed.wait(2)
    operation_id = response.json()["operation_id"]
    from mn_api.operations import stream_local_operation_events

    list(stream_local_operation_events(operation_id))
    operation = get_operation(operation_id)
    assert operation["status"] == "failed"
    assert "private installation details" not in json.dumps(operation["error"])


@pytest.mark.parametrize("remote", [False, True])
def test_docker_installation_uses_shared_sdk_and_nim_gateway(monkeypatch, remote):
    from mn_sdk import resolve_model_entry, docker_model_runner_endpoint

    entry = resolve_model_entry("cosmos3")
    monkeypatch.setattr(model_installation, "resolve_runtime_cluster_model_for_api",
                        lambda **_kw: {"node": "spark"} if remote else None)
    calls = []
    def local(model, **_options):
        calls.append(model)
        return {"entry": entry, "source": "docker"}
    def native(**kwargs):
        calls.append(kwargs["entry"]["id"])
        assert kwargs["entry"]["source"] == "docker"
        return {"install": {"source": "docker"}, "endpoint": docker_model_runner_endpoint(
            entry, node="spark", source="sdk_native_runtime_service")}
    monkeypatch.setattr(model_installation, "install_runtime_model", local)
    monkeypatch.setattr(model_installation, "install_runtime_cluster_model_for_api", native)
    monkeypatch.setattr(model_installation, "sync_runtime_model_gateways_for_api", lambda summary: summary["endpoints"])
    result = model_installation.install_model("cosmos3", {}, lambda **_kwargs: None)
    assert calls == [entry["id"]]  # No Nemotron preparation or default substitution.
    endpoint = result["endpoints"][entry["id"]]
    assert endpoint["provider"] == "openai-compatible"
    assert endpoint["api_base"] == "http://host.docker.internal:30082/v1"
    assert endpoint["api_model"] == "nvidia/cosmos3-nano-reasoner"
    assert get_registered_model(entry["id"])["kind"] == "docker"
