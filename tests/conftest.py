import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mn_api import state
from mn_api import blueprints as blueprints_module
from mn_api.main import app


@pytest.fixture(autouse=True)
def isolated_mn_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MN_HOME", str(tmp_path / "mn-home"))
    monkeypatch.delenv("MN_SHARED_STORAGE_ROOT", raising=False)
    monkeypatch.delenv("MN_HOST_SHARED_STORAGE_ROOT", raising=False)
    monkeypatch.delenv("MN_RUNTIME_SHARED_STORAGE_ROOT", raising=False)
    monkeypatch.delenv("MN_CONTAINER_SHARED_STORAGE_ROOT", raising=False)


@pytest.fixture(autouse=True)
def stub_cli_run_manifest_helpers(monkeypatch):
    def identity_manifest(manifest, *args, **kwargs):
        return manifest

    def identity_environment(environment, *args, **kwargs):
        return environment

    def no_op(*args, **kwargs):
        return None

    helpers = {
        "prepare_skill_runtime_for_manifest": identity_manifest,
        "ensure_blueprint_support_sdk_build_context_uploads": lambda manifest: {},
        "refresh_embedded_blueprint_config": no_op,
        "localize_skill_dependencies_for_dev": identity_manifest,
        "inject_skill_dependency_python_environments": identity_manifest,
        "skill_dependency_package_names": lambda manifest: set(),
        "release_skill_dependency_runtime_environment": identity_environment,
        "stage_upload_path_payloads_for_manifest": no_op,
        "stage_blueprint_support_payloads_for_manifest": no_op,
        "stage_skill_runtime_support_payloads_for_manifest": no_op,
        "stage_skill_dependency_payloads_for_manifest": no_op,
        "strip_docker_model_runner_placement_requirements": no_op,
        "normalize_host_local_uploads": no_op,
        "lower_manifest_topology_for_runtime_submission": no_op,
    }
    original = blueprints_module.import_mn_cli_helper

    def resolve_helper(module_name, function_name):
        if module_name == "mn_cli.libs.run_manifest" and function_name in helpers:
            return helpers[function_name]
        return original(module_name, function_name)

    monkeypatch.setattr(blueprints_module, "import_mn_cli_helper", resolve_helper)


@pytest.fixture
def api_client():
    return TestClient(app)


@pytest.fixture
def state_snapshot(monkeypatch):
    original_config = state.config
    original_client = state.client
    yield SimpleNamespace(config=original_config, client=original_client)
    monkeypatch.setattr(state, "config", original_config)
    monkeypatch.setattr(state, "client", original_client)


@pytest.fixture
def fake_runtime_client():
    return FakeRuntimeClient()


@pytest.fixture
def run_writer():
    return write_run_record


class FakeRuntimeClient:
    def __init__(self):
        self.calls = []
        self.jobs_payload = {"data": []}

    def deploy_job(self, manifest_json, payloads, deployment_key="", policy=None, update_policy=None, wait=False):
        self.calls.append(("deploy_job", manifest_json, payloads, deployment_key, policy or update_policy, wait))
        return json.dumps({
            "deployment_id": "dep-1",
            "deployment_key": deployment_key,
            "job_id": "job-1",
            "status": "running",
        })

    def list_deployments(self, **query):
        self.calls.append(("list_deployments", query))
        return json.dumps({"data": [{"deployment_id": "dep-1", "status": "running"}]})

    def get_deployment(self, id_or_key):
        self.calls.append(("get_deployment", id_or_key))
        return json.dumps({"deployment_id": id_or_key, "status": "running"})

    def promote_deployment(self, id_or_key):
        self.calls.append(("promote_deployment", id_or_key))
        return json.dumps({"deployment_id": id_or_key, "status": "promoted"})

    def rollback_deployment(self, id_or_key, version="", tag="", reason=""):
        self.calls.append(("rollback_deployment", id_or_key, version, tag, reason))
        return json.dumps({"deployment_id": id_or_key, "status": "rolled_back", "version": version, "tag": tag, "reason": reason})

    def pause_deployment(self, id_or_key, reason=""):
        self.calls.append(("pause_deployment", id_or_key, reason))
        return json.dumps({"deployment_id": id_or_key, "status": "paused", "reason": reason})

    def resume_deployment(self, id_or_key, reason=""):
        self.calls.append(("resume_deployment", id_or_key, reason))
        return json.dumps({"deployment_id": id_or_key, "status": "running", "reason": reason})

    def fail_deployment(self, id_or_key, reason=""):
        self.calls.append(("fail_deployment", id_or_key, reason))
        return json.dumps({"deployment_id": id_or_key, "status": "failed", "reason": reason})

    def reconcile_node(self, node_name, reason="", dry_run=False):
        self.calls.append(("reconcile_node", node_name, reason, dry_run))
        return json.dumps({"node": node_name, "status": "planned" if dry_run else "complete", "reason": reason})

    def drain_node(
        self,
        node_name,
        reason="",
        deadline="30m",
        deadline_ms=None,
        dry_run=False,
        ignore_system_jobs=True,
        wait=False,
    ):
        self.calls.append(
            ("drain_node", node_name, reason, deadline, deadline_ms, dry_run, ignore_system_jobs, wait)
        )
        return json.dumps({"node": node_name, "status": "dry_run" if dry_run else "started", "deadline_ms": deadline_ms})

    def undrain_node(self, node_name, reason="", mark_eligible=False):
        self.calls.append(("undrain_node", node_name, reason, mark_eligible))
        return json.dumps({"node": node_name, "status": "cancelled", "scheduling_eligible": mark_eligible})

    def cancel_node_drain(self, node_name, reason="", mark_eligible=False):
        self.calls.append(("cancel_node_drain", node_name, reason, mark_eligible))
        return json.dumps({"node": node_name, "status": "cancelled", "scheduling_eligible": mark_eligible})

    def set_node_maintenance(self, node_name, enabled=True, reason=""):
        self.calls.append(("set_node_maintenance", node_name, enabled, reason))
        return json.dumps({"node": node_name, "status": "maintenance", "enabled": enabled, "reason": reason})

    def get_resource(self):
        self.calls.append(("get_resource",))
        return json.dumps({"nodes": [{"cpu_cores": "2", "memory_gb": "4"}]})

    def set_resource(self, payload):
        self.calls.append(("set_resource", payload))
        return json.dumps({"ok": True, "resource": payload})

    def clear_jobs(self):
        self.calls.append(("clear_jobs",))
        return 3

    def list_jobs(self, limit=500, include_terminal=False):
        self.calls.append(("list_jobs", limit, include_terminal))
        return json.dumps(self.jobs_payload)

    def export_job_backup(self, job_id):
        self.calls.append(("export_job_backup", job_id))
        return json.dumps({"job_id": job_id}), {"manifest.json": b'{"graph_id":"g"}'}

    def restore_job_backup(self, backup_json, bundle_files, blueprint_id="", run_id=""):
        self.calls.append(("restore_job_backup", backup_json, bundle_files, blueprint_id, run_id))
        return json.dumps({"job_id": "job-restored", "blueprint_id": blueprint_id, "run_id": run_id})


def write_run_record(
    runs_root: Path,
    run_id: str,
    *,
    blueprint_id: str = "bp",
    status: str = "completed",
    final_artifact: dict | None = None,
    events: list[dict] | None = None,
) -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "blueprint_id": blueprint_id,
                "status": status,
                "started_at": "2026-07-06T00:00:00Z",
                "run_dir": str(run_dir),
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "final_artifact.json").write_text(json.dumps(final_artifact or {"score": 1}), encoding="utf-8")
    event_lines = [json.dumps(event) for event in (events or [{"ts": "2026-07-06T00:00:00Z", "type": status}])]
    (run_dir / "events.jsonl").write_text("\n".join(event_lines) + "\n", encoding="utf-8")
    return run_dir


def assert_problem(response, *, status_code: int, error: str):
    assert response.status_code == status_code
    payload = response.json()
    assert payload["error"] == error
    assert payload["status"] == status_code
    return payload
