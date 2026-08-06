from __future__ import annotations

import pytest
from fastapi import HTTPException

from mn_api import state
from mn_api.routes import system


def test_runtime_doctor_marks_probe_exceptions_critical(monkeypatch, api_client):
    monkeypatch.setattr(
        "mn_api.routes.system.collect_runtime_status",
        lambda **_kwargs: {
            "version": 2,
            "overall": "passing",
            "checked_at": "2026-07-06T00:00:00Z",
            "runtime": {},
            "endpoints": {},
            "components": [{"name": "core_grpc", "status": "passing"}],
            "nodes": {},
            "jobs": {},
            "shared_storage": {},
        },
    )
    monkeypatch.setattr("mn_api.routes.system.docker_status", lambda: (_ for _ in ()).throw(RuntimeError("docker down")))
    monkeypatch.setattr("mn_api.routes.system.litellm_gateway_health", lambda timeout=3.0: {"ok": True})

    response = api_client.get("/api/v2/runtime/doctor")

    assert response.status_code == 200
    body = response.json()
    assert body["overall"] == "critical"
    assert body["foundation"]["docker_model_runner"]["status"] == "critical"


def test_node_action_routes_proxy_runtime_service(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)

    cases = [
        ("reconcile", {"reason": "audit", "dry_run": True}, "reconcile_node"),
        ("drain", {"reason": "maint", "deadline": "10s", "dry_run": True}, "drain_node"),
        ("undrain", {"reason": "done", "mark_eligible": True}, "cancel_node_drain"),
        ("maintenance", {"reason": "patch", "enabled": False}, "set_node_maintenance"),
    ]

    for action, body, expected_call in cases:
        response = api_client.post(f"/api/v2/nodes/mirror_neuron@worker/{action}", json=body)
        assert response.status_code == 200, action
        if action in {"reconcile", "drain"}:
            call = fake_runtime_client.calls[-1]
            assert call[0] == "start_operation"
            assert call[1] == expected_call
            assert call[2]["node_name"] == "mirror_neuron@worker"
        else:
            assert fake_runtime_client.calls[-1][0] == expected_call
            assert fake_runtime_client.calls[-1][1] == "mirror_neuron@worker"


def test_resource_routes_enrich_and_strip_version(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)
    monkeypatch.setattr("mn_api.routes.system.native_service_ports", lambda: [{"name": "api", "port": "54001"}])

    listed = api_client.get("/api/v2/resource")
    posted = api_client.post("/api/v2/resource", json={"version": 2, "cpu": 50, "gpu": 25})
    put = api_client.put("/api/v2/resource", json={"version": 2, "memory": 75})

    assert listed.status_code == 200
    assert listed.json()["native_ports"] == [{"name": "api", "port": "54001"}]
    assert posted.json()["resource"] == {"cpu": 50, "gpu": 25}
    assert put.json()["resource"] == {"memory": 75}
    assert ("set_resource", {"cpu": 50, "gpu": 25}) in fake_runtime_client.calls
    assert ("set_resource", {"memory": 75}) in fake_runtime_client.calls


@pytest.mark.parametrize(
    ("value", "normalizer"),
    [
        ("", system.normalize_node_host),
        ("http://worker", system.normalize_node_host),
        ("bad host", system.normalize_node_host),
        ("worker", system.normalize_node_name),
        ("bad node", system.normalize_node_name),
        ("", system.normalize_node_token),
        ("bad token", system.normalize_node_token),
    ],
)
def test_node_input_normalizers_reject_unsafe_values(value, normalizer):
    with pytest.raises(HTTPException):
        normalizer(value)


@pytest.mark.parametrize("value", [0, -1, 65536, "bad"])
def test_grpc_port_normalizer_rejects_invalid_values(value):
    with pytest.raises(HTTPException):
        system.normalize_grpc_port(value)


def test_service_port_target_parser_handles_urls_and_host_ports():
    assert system._host_port_from_target("http://127.0.0.1:54001/api") == ("127.0.0.1", "54001")
    assert system._host_port_from_target("localhost:55051") == ("localhost", "55051")
    assert system._host_port_from_target("localhost") == ("localhost", "")


def test_restart_history_stripping_is_recursive():
    assert system._strip_restart_history(
        {"nodes": [{"name": "n", "restart_history": ["hidden"], "nested": {"restartReason": "hidden"}}]}
    ) == {"nodes": [{"name": "n", "nested": {}}]}


def test_runtime_service_error_paths_return_problem_responses(monkeypatch, api_client):
    class BrokenClient:
        def get_system_summary(self):
            raise RuntimeError("summary down")

        def set_resource(self, payload):
            raise RuntimeError("resource down")

    monkeypatch.setattr(state, "client", BrokenClient())

    summary = api_client.get("/api/v2/system/summary")
    resource = api_client.post("/api/v2/resource", json={"cpu": 10})

    assert summary.status_code == 500
    assert summary.json()["error"] == "MN_EXECUTION_FAILED"
    assert resource.status_code == 500
    assert resource.json()["error"] == "MN_EXECUTION_FAILED"


def test_network_handshake_rejects_empty_candidate_port_list():
    with pytest.raises(HTTPException) as error:
        system.network_handshake_with_fallback(host="worker", token="token", grpc_ports=[], local_host="local")
    assert error.value.detail == "Remote gRPC port must be a valid TCP port."


def test_native_service_ports_reads_runtime_config(monkeypatch):
    monkeypatch.setattr(
        "mn_api.routes.system.RuntimeConfig",
        type(
            "RuntimeConfigStub",
            (),
            {
                "from_env": staticmethod(
                    lambda: type(
                        "Config",
                        (),
                        {
                            "grpc_target": "127.0.0.1:55051",
                            "api_base_url": "http://127.0.0.1:54001/api/v2",
                            "web_ui_url": "http://localhost",
                        },
                    )()
                )
            },
        ),
    )

    ports = system.native_service_ports()

    assert ports == [
        {"name": "core_grpc", "label": "gRPC runtime", "host": "127.0.0.1", "port": "55051", "target": "127.0.0.1:55051"},
        {
            "name": "api",
            "label": "REST API",
            "host": "127.0.0.1",
            "port": "54001",
            "target": "http://127.0.0.1:54001/api/v2",
        },
    ]


def test_foundation_component_and_overall_status_helpers():
    assert system._foundation_component("explicit", lambda: {"status": "warning"})["status"] == "warning"
    assert system._foundation_component("available", lambda: {"available": True})["status"] == "passing"
    assert system._foundation_component("payload", lambda: "ok")["status"] == "passing"
    assert system._overall_status([{"status": "passing"}]) == "passing"


def test_detect_lan_ip_uses_hostname_fallback_and_loopback_default(monkeypatch):
    class LoopbackProbe:
        def connect(self, _target):
            return None

        def getsockname(self):
            return ("127.0.0.1", 53000)

        def close(self):
            return None

    monkeypatch.setattr(system.socket, "socket", lambda *_args, **_kwargs: LoopbackProbe())
    monkeypatch.setattr(system.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(system.socket, "gethostbyname", lambda _host: "192.168.1.50")

    assert system.detect_lan_ip() == "192.168.1.50"

    monkeypatch.setattr(system.socket, "gethostbyname", lambda _host: "127.0.0.1")

    assert system.detect_lan_ip() == "127.0.0.1"
