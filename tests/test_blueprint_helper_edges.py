from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mn_api import blueprints


def test_blueprint_small_helpers_normalize_inputs():
    assert blueprints.blueprint_web_ui_enabled({"web_ui": {"enabled": True}}) is True
    assert blueprints.blueprint_web_ui_enabled({"web_ui": {"enabled": False}}) is False
    assert blueprints.as_dict([]) == {}
    assert blueprints.as_list({}) == []
    assert blueprints.string_env_values({"A": 1, "B": None}) == {"A": "1"}
    assert blueprints.normalize_category_name(None) == "General"
    assert blueprints.category_slug("Tax & Finance!") == "tax-finance"
    assert blueprints.sanitize_blueprint_id("../Bad Blueprint!!") == "Bad_Blueprint"

    with pytest.raises(HTTPException):
        blueprints.validate_blueprint_id("bad id")
    with pytest.raises(HTTPException):
        blueprints.validate_run_id("bad/run")


def test_cached_git_repo_path_uses_configured_cache(monkeypatch, tmp_path):
    monkeypatch.setattr("mn_api.blueprints.config_path", lambda name, default="": tmp_path / "cache")

    path = blueprints.cached_git_repo_path("https://github.com/MirrorNeuronLab/blueprints.git")

    assert path.parent == tmp_path / "cache"
    assert path.name.startswith("blueprints-")


def test_blueprint_repo_root_validates_local_config(tmp_path):
    missing = SimpleNamespace(blueprint_source="local", blueprint_local="", active_blueprint_location="")
    with pytest.raises(HTTPException) as missing_error:
        blueprints.blueprint_repo_root(missing)
    assert missing_error.value.detail == "MN_BLUEPRINT_LOCAL is not configured"

    no_index = tmp_path / "repo"
    no_index.mkdir()
    config = SimpleNamespace(blueprint_source="local", blueprint_local=str(no_index), active_blueprint_location="")
    with pytest.raises(HTTPException) as index_error:
        blueprints.blueprint_repo_root(config)
    assert index_error.value.detail == "MN_BLUEPRINT_LOCAL index.json was not found"

    (no_index / "index.json").write_text("[]", encoding="utf-8")
    assert blueprints.blueprint_repo_root(config) == no_index.resolve()


def test_blueprint_repo_root_validates_github_config(monkeypatch, tmp_path):
    with pytest.raises(HTTPException) as missing_error:
        blueprints.blueprint_repo_root(SimpleNamespace(blueprint_source="github", blueprint_repo=""))
    assert missing_error.value.detail == "MN_BLUEPRINT_REPO is not configured"

    with pytest.raises(HTTPException) as url_error:
        blueprints.blueprint_repo_root(SimpleNamespace(blueprint_source="github", blueprint_repo="not-a-url"))
    assert url_error.value.detail == "MN_BLUEPRINT_REPO must be a Git URL"

    cached = tmp_path / "cached"
    cached.mkdir()
    monkeypatch.setattr("mn_api.blueprints.ensure_git_blueprint_repo", lambda repo_url: cached)
    assert (
        blueprints.blueprint_repo_root(
            SimpleNamespace(blueprint_source="github", blueprint_repo="https://github.com/org/repo.git")
        )
        == cached.resolve()
    )


def test_load_blueprint_catalog_rejects_non_list_index(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.json").write_text(json.dumps({"blueprints": {}}), encoding="utf-8")
    config = SimpleNamespace(blueprint_source="local", blueprint_local=str(repo), active_blueprint_location="")

    with pytest.raises(HTTPException) as error:
        blueprints.load_blueprint_catalog(config)

    assert error.value.detail == "blueprint repo index.json must be a list"


def test_normalize_blueprint_categories_and_filtering(tmp_path):
    normalized = blueprints.normalize_blueprint(
        {
            "blueprintId": "tax_bot",
            "product": {"one_line": "Does taxes", "category": "Finance"},
            "pricing": {"model": "paid", "rate": "12.5", "unit": "run"},
            "runtimeFeatures": ["web_ui"],
        }
    )

    assert normalized["id"] == "tax_bot"
    assert normalized["category_slug"] == "finance"
    assert normalized["rate_label"] == "$12.5/run"
    assert normalized["runtime_features"] == ["web_ui"]
    assert blueprints.normalize_blueprint({"name": "missing id"}) is None

    (tmp_path / "category.json").write_text(
        json.dumps({"categories": [{"name": "Finance", "slug": "Finance Ops"}]}),
        encoding="utf-8",
    )
    categories = blueprints.load_blueprint_categories(tmp_path, [normalized])
    assert categories == [{"name": "Finance", "slug": "finance-ops", "count": 0}, {"name": "Finance", "slug": "finance", "count": 1}]
    assert blueprints.filter_blueprints_by_category([normalized], "finance") == [normalized]
    assert blueprints.filter_blueprints_by_category([normalized], "sales") == []


def test_blueprint_bundle_root_and_validation_reject_bad_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(HTTPException) as escaped:
        blueprints.blueprint_bundle_root(repo, {"id": "bp", "path": "../outside"})
    assert escaped.value.detail == "blueprint path escapes repository"

    with pytest.raises(HTTPException) as missing_dir:
        blueprints.validate_blueprint_bundle(repo, {"id": "bp", "path": "missing"})
    assert missing_dir.value.detail == "blueprint bundle directory was not found"

    bundle = repo / "bp"
    bundle.mkdir()
    with pytest.raises(HTTPException) as missing_manifest:
        blueprints.validate_blueprint_bundle(repo, {"id": "bp", "path": "bp"})
    assert missing_manifest.value.detail == "blueprint bundle manifest.json was not found"

    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    assert blueprints.validate_blueprint_bundle(repo, {"id": "bp", "path": "bp"}) == bundle.resolve()


def test_validation_output_parsers(monkeypatch, tmp_path):
    monkeypatch.setattr("mn_api.blueprints.mn_base_command", lambda: ["mn"])
    assert blueprints.mn_validate_command(tmp_path) == ["mn", "blueprint", "validate", str(tmp_path), "--output", "json"]
    assert blueprints.clean_validation_output("\x1b[31m bad \x1b[0m") == "bad"
    assert blueprints.parse_validation_json('prefix {"ok": true, "value": 1} suffix') == {"ok": True, "value": 1}
    assert blueprints.parse_validation_json("[1]") is None
    assert blueprints.parse_validation_json("no json") is None
    assert blueprints.validation_failure_report(" bad ")["errors"] == ["bad"]
    assert blueprints.validation_failure_report("")["errors"] == ["mn blueprint validate failed"]


def test_agent_topology_and_openshell_path_helpers(tmp_path):
    manifest = {"agents": {"nodes": [{"node_id": "a"}, "bad"], "edges": [{"from": "a"}], "entrypoints": ["a"]}}

    assert blueprints.manifest_agent_nodes(manifest) == [{"node_id": "a"}]
    blueprints.materialize_agent_topology_for_runtime(manifest)
    assert manifest["nodes"] == [{"node_id": "a"}, "bad"]
    assert manifest["edges"] == [{"from": "a"}]
    assert manifest["entrypoints"] == ["a"]

    docker_dir = tmp_path / "payloads" / "image"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile").write_text("FROM scratch", encoding="utf-8")
    assert blueprints.openshell_local_from_path(tmp_path, "image") == docker_dir.resolve()
    assert blueprints.openshell_local_from_path(tmp_path, "https://example.com/image") is None


def test_openshell_gateway_helpers(monkeypatch, tmp_path):
    config_dir = tmp_path / "openshell"
    gateway_dir = config_dir / "gateways" / "openshell"
    gateway_dir.mkdir(parents=True)
    (config_dir / "active_gateway").write_text("active", encoding="utf-8")
    (config_dir / "gateways" / "active").mkdir()
    (config_dir / "gateways" / "active" / "metadata.json").write_text(
        json.dumps({"is_remote": False, "gateway_endpoint": "http://127.0.0.1:8080"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("mn_api.blueprints.config_value", lambda name: "")
    monkeypatch.setattr("mn_api.blueprints.openshell_config_dir", lambda: config_dir)

    assert blueprints.openshell_gateway_name() == "active"
    assert blueprints.openshell_gateway_metadata("active")["gateway_endpoint"] == "http://127.0.0.1:8080"
    assert blueprints.openshell_gateway_metadata("") == {}
    assert blueprints.openshell_gateway_uses_local_docker() is True

    monkeypatch.setattr("mn_api.blueprints.subprocess_environment", lambda: {})
    assert blueprints.openshell_env()["OPENSHELL_GATEWAY"] == "active"


def test_background_relay_config_helpers(monkeypatch):
    values = {}
    monkeypatch.setattr("mn_api.blueprints.config_optional_value", lambda name: values.get(name))

    assert blueprints.background_event_relay_poll_seconds({"web_ui": {"output": {"refresh_seconds": "0"}}}) == 0.1
    assert blueprints.background_event_relay_max_seconds({"budgets": {"max_stream_duration_seconds": "2"}}) == 2.0
    values["MN_RUN_EVENT_RELAY_POLL_SECONDS"] = "bad"
    values["MN_RUN_EVENT_RELAY_MAX_SECONDS"] = "none"
    assert blueprints.background_event_relay_poll_seconds({}) == 1.0
    assert blueprints.background_event_relay_max_seconds({}) is None
    values["MN_RUN_EVENT_RELAY_MAX_SECONDS"] = "bad"
    assert blueprints.background_event_relay_max_seconds({}) is None


def test_scheduler_job_payload_helpers():
    payload = {
        "data": [
            {
                "job_id": "active",
                "status": "running",
                "scheduler": {"placements": [{"allocations": {"ports": [{"port": "54001"}, 0, "bad", 65536]}}]},
            },
            {"id": "done", "status": "completed", "scheduler": {"placements": [{"allocations": {"ports": [55051]}}]}},
        ]
    }

    assert blueprints.active_job_ids_from_jobs_payload(payload) == {"active"}
    assert blueprints.job_dicts_from_payload({"jobs": [{"id": "job"}]}) == [{"id": "job"}]
    assert blueprints.job_id_from_payload({"summary": {"id": "summary-job"}}) == "summary-job"
    assert blueprints.scheduler_allocated_ports_from_jobs_payload(payload, active_job_ids={"active"}) == {54001}
    assert blueprints.scheduler_allocated_ports_from_jobs_payload(payload) == {54001, 55051}


def test_run_dir_blueprint_matching_and_mapping_helpers(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (run_dir / "job.json").write_text(json.dumps({"blueprint_id": "bp", "job_id": "job-1"}), encoding="utf-8")

    assert blueprints.run_dir_matches_blueprint(run_dir, blueprint_id="bp", bundle_root=bundle_root) is True
    assert blueprints.run_dir_matches_blueprint(run_dir, blueprint_id="other", bundle_root=bundle_root) is False
    assert blueprints.run_dir_job_id(run_dir) == "job-1"

    (run_dir / "job.json").unlink()
    (run_dir / "post_launch_hook.json").write_text(json.dumps({"bundle_dir": str(bundle_root)}), encoding="utf-8")
    assert blueprints.run_dir_matches_blueprint(run_dir, blueprint_id="bp", bundle_root=bundle_root.resolve()) is True

    monkeypatch.setattr("mn_api.blueprints.time.time", lambda: run_dir.stat().st_mtime + blueprints.UNMAPPED_RUN_STALE_SECONDS + 1)
    assert blueprints.unmapped_run_dir_is_stale(run_dir) is True


def test_manifest_config_and_environment_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr("mn_api.blueprints.resolve_llm_environment", lambda config: {})
    config = {
        "video_source": {"uri": "rtsp://camera", "frame_sample_seconds": 2},
        "llm": {"model": "gpt", "api_base": "http://llm", "max_tokens": 100},
    }
    env = blueprints.config_to_environment(config)
    assert env["VIDEO_SOURCE_URI"] == "rtsp://camera"
    assert env["MN_LLM_MODEL"] == "gpt"
    assert env["LITELLM_MAX_TOKENS"] == "100"

    manifest = {"nodes": [{"node_id": "worker", "config": {"environment": {"PYTHONPATH": "a"}}}]}
    blueprints.apply_manifest_config_bindings(
        manifest,
        {
            "value": {"enabled": True},
            "manifest_config_bindings": [
                {"config_path": "value.enabled", "manifest_path": "nodes.worker.config.enabled", "stringify": True},
                {"config_path": "missing", "manifest_path": "ignored"},
            ],
        },
    )
    assert manifest["nodes"][0]["config"]["enabled"] == "true"

    node_env = {"PYTHONPATH": "b", "MN_LLM_PROVIDER": "docker_model_runner", "MN_LLM_API_BASE": "http://localhost:12434/v1"}
    blueprints.inject_node_environment(manifest, node_env)
    environment = manifest["nodes"][0]["config"]["environment"]
    assert environment["PYTHONPATH"] == os.pathsep.join(["a", "b"])
    assert "host.docker.internal" in environment["MN_LLM_API_BASE"] or "localhost" not in environment["MN_LLM_API_BASE"]
    environment["LITELLM_MODEL"] = "legacy"
    blueprints.add_mn_llm_aliases(environment)
    assert environment["MN_LLM_MODEL"] == "legacy"


def test_config_loading_and_json_helpers(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    (bundle / "config" / "default.json").write_text(json.dumps({"a": {"b": 1}, "keep": True}), encoding="utf-8")
    (bundle / "config" / "overwrite.json").write_text(json.dumps({"a": {"c": 2}}), encoding="utf-8")

    assert blueprints.deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    assert blueprints.load_blueprint_config(bundle, config_overrides={"a": {"d": 3}})["a"] == {"b": 1, "c": 2, "d": 3}
    assert blueprints.load_blueprint_config_overwrites(bundle)["a"] == {"c": 2}
    assert blueprints.config_path_get({"a": {"b": 1}}, "a.b") == 1
    assert blueprints.config_path_get({"a": {}}, "a.b") is None

    bad = bundle / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(HTTPException):
        blueprints.read_json_object(bad)
