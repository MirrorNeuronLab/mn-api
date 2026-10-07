from mn_api import blueprints


def test_local_mac_preparation_requests_native_environment(tmp_path, monkeypatch):
    request = {}
    monkeypatch.setattr(blueprints, "call_prepare_runtime_model", lambda _client, attrs, **_kwargs:
                        request.update(attrs) or {"runtime_path": "/host/env", "host_path": "/host/env",
                                                  "native_target": "native-sdk:55052"})
    config = {"runner_module": "MirrorNeuron.Runner.HostLocal",
              "python_environment": {"packages": ["example==1.0"]}}
    manifest = {"runtime": {"placement": {"must_run_local": True}}, "requirements": {"os": "darwin"},
                "agents": {"nodes": [{"node_id": "collector", "config": config}]}}
    blueprints.prepare_hostlocal_python_environments_for_submission(
        tmp_path, manifest, runtime_client=object(), selected_runtime_node="local-mac",
    )
    assert request["native_host"] is True
    assert config["mn_native_host_python"] == {"python": "/host/env/bin/python", "target": "native-sdk:55052"}


def test_api_version_lookup_separates_source_extras():
    manifest = {"metadata": {"mn_local_skill_dependencies": {"sources": [
        {"source": "/source/sdk", "version": "1.3.58.dev45"},
    ]}}}
    assert blueprints.hostlocal_local_source_versions(manifest, ["/source/sdk[context]"]) == {
        "/source/sdk[context]": "1.3.58.dev45",
    }
