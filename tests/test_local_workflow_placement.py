"""Exercise local-only blueprint placement through the desktop's API adapter."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mn_sdk.blueprints.authoring import write_blueprint_definition
from mn_sdk.errors import AppError, normalize_exception
from mn_sdk.submission_placement import prepared_job_owner
from mn_sdk.submission_preparation import manifest_nodes

from mn_api import blueprints, state


@pytest.mark.parametrize("local_os,selected_node", [
    ("darwin", ""), ("darwin", "spark"), ("linux", ""), ("windows", ""), ("", ""),
])
def test_api_launch_requires_the_submitting_mac(tmp_path, monkeypatch, local_os, selected_node):
    monkeypatch.setenv("MN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MN_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.delenv("MN_SELECTED_RUNTIME_NODE", raising=False)
    bundle = tmp_path / "local_security"
    write_blueprint_definition(bundle, {
        "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "local_security",
        "name": "Local security", "description": "Test fixture",
        "runtime": {"placement": {"must_run_local": True}},
        "requirements": {"os": "darwin"}, "contract": {},
        "workflow": {"steps": [{"id": "inspect", "run": {"handler": "steps.inspect"}}]},
        "agents": {"nodes": [{"node_id": "inspect", "config": {"runner_module": "MirrorNeuron.Runner.HostLocal"}}]},
    })
    nodes = [{"name": name, "self": local, "status": "healthy", "scheduling_eligible": True,
              "hardware": {"platform": {"os": os}},
              "coordination_store": {"identity": "test-store", "writable_primary": True, "healthy": True}}
             for name, os, local in [("mini", local_os, True), ("spark", "darwin", False)]]
    report = json.dumps({"nodes": nodes})
    with (
        patch.object(state.client, "get_resource", return_value=report),
        patch.object(state.client, "get_system_summary", return_value=report),
        patch.object(blueprints, "prepare_job_submission", side_effect=lambda manifest, payloads, **kwargs:
                     SimpleNamespace(manifest_json=json.dumps(manifest), payloads=payloads)) as prepare,
    ):
        options = {"env_overrides": {"MN_SELECTED_RUNTIME_NODE": selected_node}} if selected_node else {}
        if local_os != "darwin" or selected_node:
            with pytest.raises((AppError, RuntimeError)) as raised:
                blueprints.load_blueprint_bundle(tmp_path, {"id": "local_security", "path": "local_security"}, "run-local", **options)
            message = normalize_exception(raised.value).user_message
            assert ("must run on this computer" if selected_node else "requires macOS") in message
            prepare.assert_not_called()
            return
        manifest_json, _ = blueprints.load_blueprint_bundle(
            tmp_path, {"id": "local_security", "path": "local_security"}, "run-local", **options,
        )
    manifest = json.loads(manifest_json)
    assert prepared_job_owner(manifest) == "mini"
    assert manifest["runtime"]["placement"]["must_run_local"] is True
    assert all(node["policies"]["scheduler"]["preferred_node"] == "mini" for node in manifest_nodes(manifest))
