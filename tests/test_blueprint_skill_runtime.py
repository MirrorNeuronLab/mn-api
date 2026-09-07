from __future__ import annotations

from mn_api import blueprints


def test_prepare_skill_runtime_for_submission_uses_sdk(monkeypatch, tmp_path):
    calls = []

    def prepare(manifest, config, *, bundle_dir, workspace_root):
        calls.append((manifest, config, bundle_dir, workspace_root))
        return {"prepared": True}

    monkeypatch.setattr(blueprints, "sdk_prepare_skill_runtime_for_manifest", prepare)
    manifest = {"nodes": []}
    config = {"input_skills": {}}

    assert blueprints.prepare_skill_runtime_for_submission(
        manifest,
        config,
        bundle_dir=tmp_path,
    ) == {"prepared": True}
    assert calls == [(manifest, config, tmp_path, blueprints.workspace_root())]


def test_stage_blueprint_payloads_uses_sdk_for_skill_runtime(monkeypatch, tmp_path):
    calls = []

    def stage_skill_runtime(manifest, payloads, *, bundle_dir):
        calls.append(("sdk_skill_runtime", manifest, payloads, bundle_dir))
        payloads["__mn_skill_runtime/docker_worker/Dockerfile"] = b"FROM scratch\n"

    def stage(function_name):
        def helper(manifest, payloads, *, bundle_dir):
            calls.append((function_name, manifest, payloads, bundle_dir))

        return helper

    monkeypatch.setattr("mn_sdk.skill_runtime.stage_skill_runtime_payloads_for_manifest", stage_skill_runtime)
    monkeypatch.setattr(
        __import__("mn_sdk.submission_preparation", fromlist=["stage_upload_path_payloads_for_manifest"]),
        "stage_upload_path_payloads_for_manifest",
        stage("stage_upload_path_payloads_for_manifest"),
    )
    monkeypatch.setattr(
        __import__("mn_sdk.submission_preparation", fromlist=["stage_upload_path_payloads_for_manifest"]),
        "stage_sdk_payloads_for_manifest",
        stage("stage_sdk_payloads_for_manifest"),
    )
    monkeypatch.setattr(
        __import__("mn_sdk.submission_preparation", fromlist=["stage_upload_path_payloads_for_manifest"]),
        "stage_skill_dependency_payloads_for_manifest",
        stage("stage_skill_dependency_payloads_for_manifest"),
    )
    manifest = {"metadata": {}}
    payloads: dict[str, bytes] = {}

    blueprints.stage_blueprint_payloads_for_submission(manifest, payloads, bundle_dir=tmp_path)

    assert [call[0] for call in calls] == [
        "stage_upload_path_payloads_for_manifest",
        "stage_sdk_payloads_for_manifest",
        "sdk_skill_runtime",
        "stage_skill_dependency_payloads_for_manifest",
    ]
    assert payloads["__mn_skill_runtime/docker_worker/Dockerfile"] == b"FROM scratch\n"
