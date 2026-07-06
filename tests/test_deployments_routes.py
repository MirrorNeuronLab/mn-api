from __future__ import annotations

import json

from mn_api import state


def test_deployment_create_encodes_payloads_and_policy(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)

    response = api_client.post(
        "/api/v1/deployments",
        json={
            "manifest_json": '{"graph_id":"g","nodes":[]}',
            "payloads": {"payload.txt": "hello"},
            "key": "prod",
            "wait": True,
            "policy": {
                "strategy": "canary",
                "canary": 1,
                "max_parallel": 2,
                "auto_promote": True,
                "auto_revert": True,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["deployment_key"] == "prod"
    call = fake_runtime_client.calls[0]
    assert call[0] == "deploy_job"
    assert json.loads(call[1])["graph_id"] == "g"
    assert call[2] == {"payload.txt": b"hello"}
    assert call[3] == "prod"
    assert call[4]["strategy"] == "canary"
    assert call[5] is True


def test_deployment_create_rejects_missing_manifest_before_sdk_call(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)

    response = api_client.post("/api/v1/deployments", json={"key": "prod"})

    assert response.status_code == 422
    assert fake_runtime_client.calls == []


def test_deployment_action_routes_proxy_runtime_service(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)

    cases = [
        ("get", "/api/v1/deployments", None, "list_deployments"),
        ("get", "/api/v1/deployments/prod", None, "get_deployment"),
        ("post", "/api/v1/deployments/prod/promote", {}, "promote_deployment"),
        ("post", "/api/v1/deployments/prod/rollback", {"version": "v2", "tag": "stable", "reason": "bad"}, "rollback_deployment"),
        ("post", "/api/v1/deployments/prod/pause", {"reason": "maint"}, "pause_deployment"),
        ("post", "/api/v1/deployments/prod/resume", {"reason": "done"}, "resume_deployment"),
        ("post", "/api/v1/deployments/prod/fail", {"reason": "manual"}, "fail_deployment"),
    ]

    for method, path, body, expected_call in cases:
        request = getattr(api_client, method)
        response = request(path, json=body) if body is not None else request(path)
        assert response.status_code == 200, path
        assert fake_runtime_client.calls[-1][0] == expected_call


def test_deployment_bundle_path_uses_uploaded_bundle(monkeypatch, api_client, fake_runtime_client):
    monkeypatch.setattr(state, "client", fake_runtime_client)
    monkeypatch.setattr(
        "mn_api.routes.deployments.load_uploaded_bundle",
        lambda bundle_path, upload_root: ('{"graph_id":"from-bundle"}', {"bundle.txt": b"payload"}),
    )

    response = api_client.post("/api/v1/deployments", json={"_bundle_path": "/tmp/bundle.zip", "key": "bundle"})

    assert response.status_code == 200
    call = fake_runtime_client.calls[0]
    assert json.loads(call[1])["graph_id"] == "from-bundle"
    assert call[2] == {"bundle.txt": b"payload"}
