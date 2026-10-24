from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mn_api import blueprint_additions


def test_add_catalog_blueprint_records_addition_and_public_state(monkeypatch, tmp_path: Path):
    additions_dir = tmp_path / "additions"
    repo_root = tmp_path / "catalog"
    bundle_root = repo_root / "worker-one"
    bundle_root.mkdir(parents=True)
    (bundle_root / "manifest.json").write_text("{}", encoding="utf-8")
    blueprint = {
        "id": "worker-one",
        "name": "Worker One",
        "path": "worker-one",
        "revision": "rev-1",
        "installed": False,
    }
    config = SimpleNamespace(active_blueprint_location=str(repo_root))
    progress: list[dict] = []

    monkeypatch.setenv("MN_BLUEPRINT_INSTALLS_DIR", str(additions_dir))
    monkeypatch.setattr(blueprint_additions, "find_blueprint", lambda _config, _id: (repo_root, blueprint))
    monkeypatch.setattr(blueprint_additions, "validate_blueprint_bundle", lambda *_args: bundle_root)
    monkeypatch.setattr(
        blueprint_additions,
        "defer_blueprint_runtime_models",
        lambda *_args, **_kwargs: {"ok": True, "models": [{"id": "model-one", "status": "deferred_runtime_install"}]},
    )

    result = blueprint_additions.add_catalog_blueprint(
        config,
        "worker-one",
        report_progress=lambda **value: progress.append(value),
    )

    record = json.loads((additions_dir / "worker-one.json").read_text(encoding="utf-8"))
    assert record["schema_version"] == "mn.blueprint.install.v1"
    assert record["blueprint_id"] == "worker-one"
    assert record["models"][0]["status"] == "deferred_runtime_install"
    assert record["installed_at"]
    assert result["added"] is True
    assert result["blueprint"]["added"] is True
    assert "installed" not in result["blueprint"]
    assert [item["stage"] for item in progress] == [
        "resolve_blueprint",
        "validate_blueprint",
        "prepare_runtime",
        "record_addition",
    ]


def test_add_catalog_blueprint_returns_safe_actionable_model_issues(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "catalog"
    bundle_root = repo_root / "worker-one"
    bundle_root.mkdir(parents=True)
    blueprint = {"id": "worker-one", "path": "worker-one"}
    config = SimpleNamespace(active_blueprint_location=str(repo_root))

    monkeypatch.setattr(blueprint_additions, "find_blueprint", lambda _config, _id: (repo_root, blueprint))
    monkeypatch.setattr(blueprint_additions, "validate_blueprint_bundle", lambda *_args: bundle_root)
    monkeypatch.setattr(
        blueprint_additions,
        "defer_blueprint_runtime_models",
        lambda *_args, **_kwargs: {
            "ok": False,
            "models": [{"id": "model-one", "status": "failed"}],
            "errors": ["secret internal failure at /private/runtime"],
        },
    )

    with pytest.raises(blueprint_additions.BlueprintAddError) as raised:
        blueprint_additions.add_catalog_blueprint(config, "worker-one")

    assert raised.value.code == "MN_BLUEPRINT_ADD_FAILED"
    assert raised.value.operation_issues == [
        {
            "code": "runtime_model_not_ready",
            "message": "model-one could not be prepared for this blueprint.",
            "severity": "error",
        }
    ]
    assert "/private/runtime" not in str(raised.value.operation_issues)


def test_add_catalog_blueprint_wraps_unexpected_runtime_errors(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "catalog"
    bundle_root = repo_root / "worker-one"
    bundle_root.mkdir(parents=True)
    blueprint = {"id": "worker-one", "path": "worker-one"}
    config = SimpleNamespace(active_blueprint_location=str(repo_root))

    monkeypatch.setattr(blueprint_additions, "find_blueprint", lambda _config, _id: (repo_root, blueprint))
    monkeypatch.setattr(blueprint_additions, "validate_blueprint_bundle", lambda *_args: bundle_root)
    monkeypatch.setattr(
        blueprint_additions,
        "defer_blueprint_runtime_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("token=secret at /private/runtime")),
    )

    with pytest.raises(blueprint_additions.BlueprintAddError) as raised:
        blueprint_additions.add_catalog_blueprint(config, "worker-one")

    assert raised.value.code == "MN_BLUEPRINT_ADD_FAILED"
    assert "secret" not in raised.value.user_message
    assert "/private/runtime" not in raised.value.user_message


def test_addition_passes_invocation_mode_to_runtime_preparation(monkeypatch, tmp_path):
    blueprint = {"id": "worker", "path": "worker"}
    config = SimpleNamespace(active_blueprint_location=str(tmp_path))
    monkeypatch.setenv("MN_BLUEPRINT_INSTALLS_DIR", str(tmp_path / "additions"))
    monkeypatch.setattr(blueprint_additions, "find_blueprint", lambda *_a: (tmp_path, blueprint))
    monkeypatch.setattr(blueprint_additions, "validate_blueprint_bundle", lambda *_a: tmp_path)
    seen = []
    def prepare(*_a, **kwargs):
        seen.append(kwargs)
        return {"ok": True, "models": [], "services": []}
    monkeypatch.setattr(blueprint_additions, "defer_blueprint_runtime_models", prepare)
    overrides = {"llm": {"mode": "fake"}, "execution": {"fake_skills": True}}
    result = blueprint_additions.add_catalog_blueprint(config, "worker", config_overrides=overrides)
    assert seen == [{"force": False, "config_overrides": overrides}]
    assert result["added"] is True
    record = json.loads((tmp_path / "additions/worker.json").read_text())
    assert "config_overrides" not in record
