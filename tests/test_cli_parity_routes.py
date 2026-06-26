import base64
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from mn_api import state
from mn_api.main import app


class FakeRuntimeClient:
    def __init__(self):
        self.calls = []

    def deploy_job(self, manifest_json, payloads, deployment_key="", update_policy=None, wait=False):
        self.calls.append(("deploy_job", manifest_json, payloads, deployment_key, update_policy, wait))
        return json.dumps({"deployment_id": "dep-1", "deployment_key": deployment_key, "job_id": "job-1", "status": "running"})

    def list_deployments(self, **query):
        return json.dumps({"data": [{"deployment_id": "dep-1"}]})

    def get_deployment(self, id_or_key):
        return json.dumps({"deployment_id": id_or_key, "status": "running"})

    def promote_deployment(self, id_or_key):
        return json.dumps({"deployment_id": id_or_key, "status": "promoted"})

    def rollback_deployment(self, id_or_key, version="", tag="", reason=""):
        return json.dumps({"deployment_id": id_or_key, "version": version, "tag": tag, "reason": reason, "status": "rolled_back"})

    def pause_deployment(self, id_or_key, reason=""):
        return json.dumps({"deployment_id": id_or_key, "status": "paused", "reason": reason})

    def resume_deployment(self, id_or_key, reason=""):
        return json.dumps({"deployment_id": id_or_key, "status": "running", "reason": reason})

    def fail_deployment(self, id_or_key, reason=""):
        return json.dumps({"deployment_id": id_or_key, "status": "failed", "reason": reason})

    def reconcile_node(self, node_name, reason="", dry_run=False):
        return json.dumps({"node": node_name, "status": "planned" if dry_run else "complete", "reason": reason})

    def drain_node(self, node_name, reason="", deadline_ms=0, dry_run=False, ignore_system_jobs=True, wait=False):
        self.calls.append(("drain_node", node_name, deadline_ms, dry_run, ignore_system_jobs, wait))
        return json.dumps({"node": node_name, "status": "dry_run" if dry_run else "started", "deadline_ms": deadline_ms})

    def cancel_node_drain(self, node_name, reason="", mark_eligible=False):
        return json.dumps({"node": node_name, "status": "cancelled", "scheduling_eligible": mark_eligible})

    def set_node_maintenance(self, node_name, enabled, reason=""):
        return json.dumps({"node": node_name, "status": "maintenance", "enabled": enabled, "reason": reason})

    def export_job_backup(self, job_id):
        return json.dumps({"job_id": job_id}), {"manifest.json": b'{"graph_id":"g"}'}

    def restore_job_backup(self, backup_json, bundle_files, blueprint_id="", run_id=""):
        return json.dumps({"job_id": "job-restored", "blueprint_id": blueprint_id, "bundle_size": len(bundle_files)})


def test_deployment_routes_use_shared_policy(monkeypatch):
    fake = FakeRuntimeClient()
    monkeypatch.setattr(state, "client", fake)
    client = TestClient(app)

    response = client.post(
        "/api/v1/deployments",
        json={
            "manifest_json": '{"graph_id":"g","nodes":[]}',
            "key": "prod",
            "policy": {"strategy": "canary", "canary": 1, "max_parallel": 2, "auto_promote": True, "auto_revert": True},
        },
    )

    assert response.status_code == 200
    assert response.json()["deployment_key"] == "prod"
    assert fake.calls[0][4]["strategy"] == "canary"
    assert fake.calls[0][4]["max_parallel"] == 2


def test_node_drain_route_parses_cli_duration(monkeypatch):
    fake = FakeRuntimeClient()
    monkeypatch.setattr(state, "client", fake)
    client = TestClient(app)

    response = client.post("/api/v1/nodes/mirror_neuron@worker/drain", json={"deadline": "10s", "dry_run": True})

    assert response.status_code == 200
    assert response.json()["deadline_ms"] == 10_000
    assert fake.calls[0] == ("drain_node", "mirror_neuron@worker", 10_000, True, True, False)


def test_job_backup_restore_routes_encode_bundle_bytes(monkeypatch):
    monkeypatch.setattr(state, "client", FakeRuntimeClient())
    client = TestClient(app)

    backup = client.post("/api/v1/jobs/job-1/backup")
    assert backup.status_code == 200
    encoded = backup.json()["bundle_files"]["manifest.json"]
    assert base64.b64decode(encoded) == b'{"graph_id":"g"}'

    restore = client.post(
        "/api/v1/jobs/restore",
        json={"backup_json": backup.json()["backup_json"], "bundle_files": {"manifest.json": encoded}, "blueprint_id": "bp"},
    )
    assert restore.status_code == 200
    assert restore.json()["job_id"] == "job-restored"


def test_api_and_cli_resource_paths_share_sdk_logic(monkeypatch):
    cli_root = Path(__file__).resolve().parents[2] / "mn-cli"
    if str(cli_root) not in sys.path:
        sys.path.insert(0, str(cli_root))
    from mn_cli.libs.resource_cmds import ensure_combined_resource_totals as cli_normalize
    from mn_api.routes.system import ensure_combined_resource_totals as api_normalize

    payload = {"nodes": [{"cpu_cores": "2", "memory_gb": "4"}]}

    assert cli_normalize(payload) == api_normalize(payload)
