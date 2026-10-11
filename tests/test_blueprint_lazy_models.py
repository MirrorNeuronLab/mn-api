from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mn_api import blueprint_additions, blueprints
from mn_api.routes import blueprints as routes


@pytest.fixture
def lazy_blueprint(monkeypatch, tmp_path: Path):
    blueprint = {"id": "worker", "path": "worker"}
    bundle = tmp_path / "worker"
    bundle.mkdir()
    monkeypatch.setattr(blueprint_additions, "find_blueprint", lambda *_args: (tmp_path, blueprint))
    monkeypatch.setattr(routes, "validate_blueprint_hardware_requirements", lambda *_args, **_kwargs: {"ok": True})
    installer = Mock(side_effect=AssertionError("Early validation must not install models"))
    for name in (
        "install_blueprint_runtime_models",
        "install_model_entry",
        "install_runtime_cluster_model_for_api",
        "sync_runtime_model_gateways_for_api",
        "fanout_runtime_model_gateways_for_api",
    ):
        monkeypatch.setattr(blueprints, name, installer)
    monkeypatch.setattr(routes, "install_blueprint_runtime_models", installer)
    progress = []
    monkeypatch.setattr(
        routes,
        "record_launch_progress",
        lambda _id, phase, status, message, *args, **kwargs: progress.append(
            {"phase": phase, "status": status, "message": message, **kwargs}
        ),
    )

    def write(model: str, vendor: str = "nvidia", memory: int = 131072):
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "apiVersion": "mn.workflow/v1",
                    "kind": "Workflow",
                    "id": "worker",
                    "contract": {},
                    "agents": {},
                    "nodes": [],
                    "runtime": {
                        "models": {"primary": {"provider": "docker_model_runner", "model": model, "required": True}}
                    },
                }
            )
        )
        monkeypatch.setattr(
            blueprints,
            "runtime_resource_report",
            lambda: {
                "nodes": [
                    {
                        "name": "local",
                        "status": "healthy",
                        "scheduling_eligible": True,
                        "devices": [
                            {
                                "kind": "gpu",
                                "type": "integrated_gpu" if vendor == "apple" else "gpu",
                                "vendor": vendor,
                                "memory_total_mb": memory,
                            }
                        ],
                    }
                ]
            },
        )
        return tmp_path, blueprint, SimpleNamespace(active_blueprint_location=str(tmp_path))

    yield write, installer, progress
    installer.assert_not_called()


@pytest.mark.parametrize("operation", ["add", "launch"])
@pytest.mark.parametrize(
    "model,vendor,memory",
    [
        ("default", "apple", 16384),
        ("nemotron-3.5-lightning:latest", "nvidia", 131072),
    ],
)
def test_blueprint_validates_without_installing_and_keeps_model_policy(
    lazy_blueprint, operation, model, vendor, memory
):
    write, _, progress = lazy_blueprint
    repo, blueprint, config = write(model, vendor, memory)
    if operation == "add":
        result = blueprint_additions.add_catalog_blueprint(config, "worker")
        summary = result["runtime_preparation"]
        record = json.loads(blueprint_additions.blueprint_add_record_path("worker").read_text())
        assert record["models"] == summary["models"]
    else:
        result = routes.run_launch_preflight(repo, blueprint, progress_id="lazy", force=False, config_overrides={})
        summary = result.model_install
        assert result.config_overrides == {}
        assert "MN_MODEL_ENDPOINTS_JSON" not in result.env_overrides
        phases = [item for item in progress if item["phase"] == "model_install"]
        assert [item["status"] for item in phases] == ["running", "completed"]
        assert "without installing" in phases[0]["detail"]
        assert "deferred to first use" in phases[1]["message"]
    assert summary["ok"] is True
    assert summary["deferred"] is True
    assert summary["endpoints"] == {}
    assert summary["config_overrides"] is None
    declaration = summary["models"][0]
    assert declaration["model"] == model
    assert declaration["install_policy"] == "on_first_model_call"
    if model == "default":
        assert summary["env"]["MN_LLM_MODEL"] == "default"
        assert declaration["selection_policy"] == ["nemotron-3.5-lightning:latest", "gemma4:e2b"]
    else:
        assert declaration["selection_policy"] == [model]


@pytest.mark.parametrize("operation", ["add", "launch"])
@pytest.mark.parametrize("model", ["unknown-model:latest", "nemotron-3.5-lightning:latest"])
def test_early_validation_still_rejects_unknown_or_incompatible_models(lazy_blueprint, operation, model):
    write, _, _ = lazy_blueprint
    repo, blueprint, config = write(model, "apple", 16384)
    if operation == "add":
        with pytest.raises(blueprint_additions.BlueprintAddError):
            blueprint_additions.add_catalog_blueprint(config, "worker")
        assert not blueprint_additions.blueprint_add_record_path("worker").exists()
    else:
        result = routes.run_launch_preflight(repo, blueprint, progress_id="invalid", force=False, config_overrides={})
        assert result.status_code == 422
