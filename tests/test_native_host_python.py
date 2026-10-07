import pytest

from mn_api import blueprints


def test_local_mac_preparation_requests_native_environment(tmp_path, monkeypatch):
    request = {}
    monkeypatch.setattr(blueprints, "call_prepare_runtime_model", lambda _client, attrs, **_kwargs:
                        request.update(attrs) or {"runtime_path": "/host/env", "host_path": "/host/env",
                                                  "native_target": "native-sdk:55052", "bridge_runtime_path": "/core/bridge",
                                                  "native_host_protocol": "mn.native.host-python.v1"})
    config = {"runner_module": "MirrorNeuron.Runner.HostLocal",
              "python_environment": {"packages": ["example==1.0"]}}
    manifest = {"runtime": {"placement": {"must_run_local": True}}, "requirements": {"os": "darwin"},
                "agents": {"nodes": [{"node_id": "collector", "config": config}]}}
    blueprints.prepare_hostlocal_python_environments_for_submission(
        tmp_path, manifest, runtime_client=object(), selected_runtime_node="local-mac",
    )
    assert request["native_host"] is True
    assert config["mn_native_host_python"]["python"] == "/host/env/bin/python"
    assert config["python_environment"]["path"] == "/core/bridge"


def test_api_version_lookup_separates_source_extras():
    manifest = {"metadata": {"mn_local_skill_dependencies": {"sources": [
        {"source": "/source/sdk", "version": "1.3.58.dev45"},
    ]}}}
    assert blueprints.hostlocal_local_source_versions(manifest, ["/source/sdk[context]"]) == {
        "/source/sdk[context]": "1.3.58.dev45",
    }


def test_mac_submission_rejects_native_service_without_host_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr(blueprints, "call_prepare_runtime_model", lambda *_args, **_kwargs:
                        {"runtime_path": "/host/env", "host_path": "/host/env"})
    config = {"runner_module": "MirrorNeuron.Runner.HostLocal",
              "python_environment": {"packages": ["example==1.0"]}}
    manifest = {"runtime": {"placement": {"must_run_local": True}}, "requirements": {"os": "darwin"},
                "agents": {"nodes": [{"node_id": "collector", "config": config}]}}
    with pytest.raises(RuntimeError, match="Update the native SDK and restart"):
        blueprints.prepare_hostlocal_python_environments_for_submission(
            tmp_path, manifest, runtime_client=object(), selected_runtime_node="local-mac",
        )
    assert "mn_native_host_python" not in config
