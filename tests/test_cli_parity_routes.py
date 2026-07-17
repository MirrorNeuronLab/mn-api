import base64
import ast
import json
from pathlib import Path

import pytest

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
    if not cli_root.is_dir():
        pytest.skip("mn-cli sibling repository is not available")
    from mn_api.routes.system import ensure_combined_resource_totals as api_normalize
    from mn_sdk import ensure_combined_resource_totals as sdk_normalize

    resource_cmds = cli_root / "mn_cli" / "libs" / "resource_cmds.py"
    assert "from mn_sdk import ensure_combined_resource_totals" in resource_cmds.read_text(encoding="utf-8")

    payload = {"nodes": [{"cpu_cores": "2", "memory_gb": "4"}]}

    assert sdk_normalize(payload) == api_normalize(payload)


def test_cli_command_inventory_is_classified_for_api_parity():
    commands = _cli_command_inventory()
    classified = API_COVERED_COMMANDS | API_UNSUPPORTED_LOCAL_COMMANDS
    assert commands - classified == set()
    assert API_COVERED_COMMANDS - commands == set()


API_COVERED_COMMANDS = {
    "blueprint cleanup",
    "blueprint compare",
    "blueprint doctor",
    "blueprint export",
    "blueprint human",
    "blueprint human ack",
    "blueprint human respond",
    "blueprint install",
    "blueprint list",
    "blueprint logs",
    "blueprint monitor",
    "blueprint resources",
    "blueprint run",
    "blueprint stream",
    "blueprint tail",
    "blueprint uninstall",
    "blueprint update",
    "blueprint validate",
    "deployment deploy",
    "deployment fail",
    "deployment list",
    "deployment pause",
    "deployment promote",
    "deployment resume",
    "deployment rollback",
    "deployment status",
    "event emit",
    "event list",
    "job backup",
    "job cancel",
    "job cancel-all",
    "job clear",
    "job dead-letters",
    "job list",
    "job monitor",
    "job pause",
    "job restore",
    "job result",
    "job resume",
    "job status",
    "job submit",
    "job unfinished",
    "model doctor",
    "model install",
    "model list",
    "model proxy",
    "model remote add",
    "model remote list",
    "model remote remove",
    "model remove",
    "model show",
    "model update",
    "node add",
    "node drain",
    "node join",
    "node leave",
    "node list",
    "node maintenance",
    "node reconcile",
    "node undrain",
    "resource list",
    "resource ports",
    "resource set",
    "runtime doctor",
    "runtime health",
    "runtime metrics",
    "runtime status",
    "schedule create",
    "schedule delay",
    "schedule delete",
    "schedule list",
    "schedule pause",
    "schedule resume",
    "schedule run-now",
    "schedule status",
    "service check",
    "service list",
    "service resolve",
    "trigger create",
    "trigger delete",
    "trigger list",
}


API_UNSUPPORTED_LOCAL_COMMANDS = {
    "node expose",
    "node refresh-token",
    "runtime ensure-context-engine",
    "runtime restart-sidecars",
    "runtime start",
    "runtime stop",
    "runtime update",
}


APP_GROUPS = {
    "blueprint_app": "blueprint",
    "deployment_app": "deployment",
    "deployment_cmds.deployment_app": "deployment",
    "event_app": "event",
    "human_app": "blueprint human",
    "job_app": "job",
    "model_app": "model",
    "node_app": "node",
    "remote_app": "model remote",
    "resource_app": "resource",
    "resource_cmds.resource_app": "resource",
    "runtime_app": "runtime",
    "schedule_app": "schedule",
    "service_app": "service",
    "service_cmds.service_app": "service",
    "trigger_app": "trigger",
}


def _cli_command_inventory() -> set[str]:
    cli_root = Path(__file__).resolve().parents[2] / "mn-cli" / "mn_cli"
    if not cli_root.is_dir():
        pytest.skip("mn-cli sibling repository is not available")
    files = [cli_root / "main.py", *sorted((cli_root / "libs").glob("*.py"))]
    commands: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    if _is_command_decorator(decorator):
                        group = APP_GROUPS.get(ast.unparse(decorator.func.value))
                        if group:
                            commands.add(f"{group} {_decorated_command_name(decorator, node.name)}")
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if (
                    isinstance(call.func, ast.Call)
                    and isinstance(call.func.func, ast.Attribute)
                    and call.func.func.attr == "command"
                ):
                    group = APP_GROUPS.get(ast.unparse(call.func.func.value))
                    if group:
                        commands.add(f"{group} {_called_command_name(call)}")
    return commands


def _is_command_decorator(value: ast.AST) -> bool:
    return isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "command"


def _decorated_command_name(decorator: ast.Call, function_name: str) -> str:
    if decorator.args and isinstance(decorator.args[0], ast.Constant):
        return str(decorator.args[0].value)
    for keyword in decorator.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return function_name.replace("_", "-")


def _called_command_name(call: ast.Call) -> str:
    command_call = call.func
    if command_call.args and isinstance(command_call.args[0], ast.Constant):
        return str(command_call.args[0].value)
    for keyword in command_call.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    if call.args:
        return ast.unparse(call.args[0]).split(".")[-1].replace("_", "-")
    return "unknown"
