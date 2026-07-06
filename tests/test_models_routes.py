from __future__ import annotations

def test_model_runtime_routes_call_sdk_helpers(monkeypatch, api_client):
    calls = []
    monkeypatch.setattr("mn_api.routes.models.list_runtime_models", lambda installed_only=False: {"installed_only": installed_only})
    monkeypatch.setattr(
        "mn_api.routes.models.show_runtime_model",
        lambda model_id, compatibility=False: calls.append(("show", model_id, compatibility)) or {"model_id": model_id},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.doctor_runtime_model",
        lambda model_id: calls.append(("doctor", model_id)) or {"model_id": model_id, "ok": True},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.install_runtime_model",
        lambda model_id, backend="auto", context_size=None, force=False: calls.append(
            ("install", model_id, backend, context_size, force)
        )
        or {"model_id": model_id},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.update_runtime_model",
        lambda model_id, all_models=False, force=False: calls.append(("update", model_id, all_models, force))
        or {"updated": model_id or "all"},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.remove_runtime_model",
        lambda model_id, force=False: calls.append(("remove", model_id, force)) or {"removed": model_id},
    )

    assert api_client.get("/api/v1/models/catalog").json()["installed_only"] is False
    assert api_client.get("/api/v1/models/gemma4?compatibility=true").json()["model_id"] == "gemma4"
    assert api_client.get("/api/v1/models/gemma4/doctor").json()["ok"] is True
    assert api_client.post("/api/v1/models/gemma4/install", json={"backend": "docker", "context_size": 8192, "force": True}).json()[
        "status"
    ] == "running"
    assert api_client.post("/api/v1/models/gemma4/update", json={"force": True}).json()["updated"] == "gemma4"
    assert api_client.post("/api/v1/models:update", json={"force": True}).json()["updated"] == "all"
    assert api_client.delete("/api/v1/models/gemma4?force=true").json()["removed"] == "gemma4"
    assert api_client.post("/api/v1/models/gemma4/remove", json={"force": True}).json()["removed"] == "gemma4"

    assert ("show", "gemma4", True) in calls
    assert ("doctor", "gemma4") in calls
    assert ("install", "gemma4", "docker", 8192, True) in calls
    assert ("update", "gemma4", False, True) in calls
    assert ("update", None, True, True) in calls
    assert ("remove", "gemma4", True) in calls


def test_model_remote_and_proxy_routes_sync_gateway_when_requested(monkeypatch, api_client, tmp_path):
    calls = []
    monkeypatch.setattr("mn_api.routes.models.default_model_remotes_path", lambda: tmp_path / "remotes.json")
    monkeypatch.setattr("mn_api.routes.models.default_model_proxies_path", lambda: tmp_path / "proxies.json")
    monkeypatch.setattr(
        "mn_api.routes.models.load_model_remotes",
        lambda: {"remotes": {"spark": {"name": "spark", "model": "ai/qwen3-coder"}}},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.upsert_model_remote",
        lambda name, model, base_url, api_key="not-needed", api_model=None, node=None: calls.append(
            ("upsert_remote", name, model, base_url, api_key, api_model, node)
        )
        or {"name": name, "model": model, "base_url": base_url},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.remove_model_remote",
        lambda name: calls.append(("remove_remote", name)) or {"name": name, "model": "ai/qwen3-coder"},
    )
    monkeypatch.setattr("mn_api.routes.models.remove_litellm_gateway_route", lambda name: calls.append(("remove_route", name)))
    monkeypatch.setattr("mn_api.routes.models.sync_litellm_gateway", lambda restart=True: calls.append(("sync", restart)) or {"ok": True})
    monkeypatch.setattr(
        "mn_api.routes.models.upsert_model_proxy",
        lambda model_id, **kwargs: calls.append(("upsert_proxy", model_id, kwargs)) or {"id": model_id},
    )
    monkeypatch.setattr(
        "mn_api.routes.models.resolve_model_entry",
        lambda model: {"id": "qwen", "model": model, "api_model": "qwen-api"},
    )

    listed = api_client.get("/api/v1/models/remotes")
    added = api_client.post(
        "/api/v1/models/remotes",
        json={"model": "ai/qwen3-coder", "base_url": "http://runtime:12434/v1", "name": "spark", "sync_gateway": True},
    )
    removed = api_client.delete("/api/v1/models/remotes/spark?sync_gateway=true")
    proxied = api_client.post(
        "/api/v1/models/proxies",
        json={"model_id": "openai/gpt-4.1", "base_url": "http://127.0.0.1:4000/v1", "sync_gateway": True},
    )

    assert listed.status_code == 200
    assert listed.json()["path"] == str(tmp_path / "remotes.json")
    assert added.json()["gateway"] == {"ok": True}
    assert removed.json()["removed"]["name"] == "spark"
    assert proxied.json()["path"] == str(tmp_path / "proxies.json")
    assert ("sync", True) in calls
    assert ("remove_route", "spark") in calls
    assert ("remove_route", "ai/qwen3-coder") in calls
    assert calls[-2][0] == "upsert_proxy"


def test_model_benchmark_rejects_missing_or_non_docker_models(monkeypatch, api_client):
    monkeypatch.setattr(
        "mn_api.routes.models.load_model_catalog",
        lambda: {
            "gemma4": {"id": "gemma4", "model": "ai/gemma4:e2b", "provider": "docker_model_runner"},
            "proxy": {"id": "proxy", "model": "openai/gpt-4.1", "provider": "litellm_proxy"},
        },
    )
    monkeypatch.setattr(
        "mn_api.routes.models._installed_model_names",
        lambda: {"available": True, "models": {"openai/gpt-4.1"}, "warnings": []},
    )

    missing = api_client.post("/api/v1/models/gemma4/benchmark")
    non_docker = api_client.post("/api/v1/models/proxy/benchmark")

    assert missing.status_code == 404
    assert "is not installed" in missing.json()["detail"]
    assert non_docker.status_code == 400
    assert "does not expose" in non_docker.json()["detail"]


def test_bounded_int_clamps_invalid_values():
    from mn_api.routes.models import _bounded_int

    assert _bounded_int(None, default=96, minimum=16, maximum=512) == 96
    assert _bounded_int("bad", default=96, minimum=16, maximum=512) == 96
    assert _bounded_int(1, default=96, minimum=16, maximum=512) == 16
    assert _bounded_int(9999, default=96, minimum=16, maximum=512) == 512
