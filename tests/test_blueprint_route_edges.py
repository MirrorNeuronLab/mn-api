from __future__ import annotations

import json
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

from mn_api import state
from mn_api.routes import blueprints


def test_blueprint_cleanup_runs_shared_resource_cleanup(monkeypatch, api_client, tmp_path):
    calls = []
    monkeypatch.setattr("mn_api.routes.blueprints.resolve_blueprint_storage", lambda source=None: tmp_path / "catalog")
    monkeypatch.setattr("mn_api.routes.blueprints.blueprint_ids_from_storage", lambda storage: {"active-bp"})
    monkeypatch.setattr(
        "mn_api.routes.blueprints.cleanup_blueprint_resources",
        lambda **kwargs: calls.append(kwargs) or {"dry_run": kwargs["dry_run"]},
    )

    dry_run = api_client.post("/api/v1/blueprints:cleanup", json={"dry_run": True})
    explicit = api_client.post("/api/v1/blueprints:cleanup", json={"blueprint_id": "bp"})

    assert dry_run.status_code == 200
    assert dry_run.json()["status"] == "planned"
    assert dry_run.json()["active_blueprint_ids"] == ["active-bp"]
    assert explicit.status_code == 200
    assert explicit.json()["status"] == "completed"
    assert calls[0]["active_blueprint_ids"] == {"active-bp"}
    assert calls[1]["blueprint_ids"] == {"bp"}


def test_blueprint_cleanup_stale_process_scope(monkeypatch, api_client, tmp_path):
    calls = []
    monkeypatch.setattr("mn_api.routes.blueprints.find_blueprint", lambda config, blueprint_id: (tmp_path, {"id": blueprint_id}))
    monkeypatch.setattr("mn_api.routes.blueprints.runtime_active_job_ids", lambda: {"active-job"})
    monkeypatch.setattr(
        "mn_api.routes.blueprints.cleanup_stale_blueprint_run_processes",
        lambda repo_root, blueprint, active_job_ids=None, reason="": calls.append((repo_root, blueprint, active_job_ids, reason)),
    )

    response = api_client.post(
        "/api/v1/blueprints:cleanup",
        json={"blueprint_id": "bp", "include_files": False, "include_docker": False},
    )

    assert response.status_code == 200
    assert response.json()["stale_processes"] is True
    assert calls == [(tmp_path, {"id": "bp"}, {"active-job"}, "api_blueprint_cleanup")]


def test_blueprint_update_pulls_storage_and_cleans_removed_blueprints(monkeypatch, api_client, tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "index.json").write_text(json.dumps([{"id": "old-bp"}, {"id": "keep-bp"}]), encoding="utf-8")
    cleanups = []

    def fake_run_git(_args):
        (storage / "index.json").write_text(json.dumps([{"id": "keep-bp"}]), encoding="utf-8")
        return SimpleNamespace(stdout="updated", stderr="")

    monkeypatch.setattr("mn_api.routes.blueprints.run_git", fake_run_git)
    monkeypatch.setattr(
        "mn_api.routes.blueprints.cleanup_blueprint_resources",
        lambda **kwargs: cleanups.append(kwargs) or {"dry_run": kwargs["dry_run"]},
    )

    update = api_client.post("/api/v1/blueprints:update", json={"source": str(storage)})

    assert update.status_code == 200
    assert update.json()["blueprints_removed"] == ["old-bp"]
    assert cleanups[0]["blueprint_ids"] == {"old-bp"}
    assert cleanups[0]["active_blueprint_ids"] == {"keep-bp"}


def test_blueprint_uninstall_dry_run_archives_and_plans_cleanup(monkeypatch, api_client, tmp_path):
    monkeypatch.setattr("mn_api.routes.blueprints.resolve_mn_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        "mn_api.routes.blueprints.cleanup_blueprint_resources",
        lambda **kwargs: {"dry_run": kwargs["dry_run"], "blueprint_ids": sorted(kwargs["blueprint_ids"])},
    )
    monkeypatch.setattr(
        "mn_api.routes.blueprints.projected_orphaned_models",
        lambda blueprint_id: [{"docker_model": f"{blueprint_id}-model"}],
    )

    uninstall = api_client.post(
        "/api/v1/blueprints:uninstall",
        json={"blueprint_id": "bp", "source": str(tmp_path / "storage"), "dry_run": True, "remove_models": True},
    )

    assert uninstall.status_code == 200
    body = uninstall.json()
    assert body["status"] == "planned"
    assert body["blueprint_id"] == "bp"
    assert "/home/blueprint_installs/archive/bp-" in body["archive"]
    assert body["cleanup"]["blueprint_ids"] == ["bp"]
    assert body["models"]["removed"] == ["bp-model"]


def test_blueprint_list_and_health_refresh_local_source_env(monkeypatch, api_client, tmp_path):
    catalog_a = tmp_path / "catalog-a"
    catalog_b = tmp_path / "catalog-b"
    catalog_a.mkdir()
    catalog_b.mkdir()
    (catalog_a / "index.json").write_text(json.dumps([{"id": "bp-a", "name": "Blueprint A"}]), encoding="utf-8")
    (catalog_b / "index.json").write_text(json.dumps([{"id": "bp-b", "name": "Blueprint B"}]), encoding="utf-8")

    monkeypatch.setenv("MN_ENV", "dev")
    monkeypatch.setenv("MN_BLUEPRINT_SOURCE", "local")
    monkeypatch.setenv("MN_BLUEPRINT_LOCAL", str(catalog_a))
    first = api_client.get("/api/v1/blueprints")

    monkeypatch.setenv("MN_BLUEPRINT_LOCAL", str(catalog_b))
    second = api_client.get("/api/v1/blueprints")
    health = api_client.get("/api/v1/health")

    assert first.status_code == 200
    assert first.json()["repo_dir"] == str(catalog_a.resolve())
    assert [blueprint["id"] for blueprint in first.json()["blueprints"]] == ["bp-a"]
    assert second.status_code == 200
    assert second.json()["repo_dir"] == str(catalog_b.resolve())
    assert [blueprint["id"] for blueprint in second.json()["blueprints"]] == ["bp-b"]
    assert health.status_code == 200
    assert health.json()["blueprint_source"] == "local"
    assert health.json()["active_blueprint_location"] == str(catalog_b.resolve())


def test_blueprint_list_and_health_use_persisted_local_source_env(monkeypatch, api_client, tmp_path):
    mn_home = tmp_path / "mn-home"
    catalog = tmp_path / "catalog"
    mn_home.mkdir()
    catalog.mkdir()
    (catalog / "index.json").write_text(json.dumps([{"id": "bp-persisted", "name": "Persisted"}]), encoding="utf-8")
    (mn_home / "docker-compose.env").write_text(
        "MN_ENV=dev\n"
        "MN_BLUEPRINT_SOURCE=local\n"
        "MN_BLUEPRINT_REPO=https://github.com/MirrorNeuronLab/mn-blueprints.git\n"
        f"MN_BLUEPRINT_LOCAL={catalog}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MN_HOME", str(mn_home))
    monkeypatch.delenv("MN_BLUEPRINT_SOURCE", raising=False)
    monkeypatch.delenv("MN_BLUEPRINT_REPO", raising=False)
    monkeypatch.delenv("MN_BLUEPRINT_LOCAL", raising=False)

    blueprints_response = api_client.get("/api/v1/blueprints")
    health_response = api_client.get("/api/v1/health")

    assert blueprints_response.status_code == 200
    assert blueprints_response.json()["repo_dir"] == str(catalog.resolve())
    assert [blueprint["id"] for blueprint in blueprints_response.json()["blueprints"]] == ["bp-persisted"]
    assert health_response.status_code == 200
    assert health_response.json()["blueprint_source"] == "local"
    assert health_response.json()["active_blueprint_location"] == str(catalog.resolve())


def test_launch_progress_helpers_record_read_and_summarize(monkeypatch, tmp_path):
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path)

    assert blueprints.validate_progress_id(None) is None
    assert blueprints.validate_progress_id(" launch-1 ") == "launch-1"
    with pytest.raises(HTTPException):
        blueprints.validate_progress_id("bad id")

    blueprints.record_launch_progress("launch-1", "resolve_source", "running", "Resolving", label="Resolve")
    blueprints.record_launch_progress("launch-1", "launch", "completed", "Done", {"job_id": "job-1"})
    (tmp_path / "launch-1.jsonl").write_text((tmp_path / "launch-1.jsonl").read_text() + "bad-json\n", encoding="utf-8")

    events = blueprints.read_launch_progress("launch-1")
    phases = blueprints.summarize_launch_progress_phases(events)

    assert len(events) == 2
    assert phases[-1]["id"] == "launch"
    assert phases[-1]["status"] == "completed"
    assert blueprints.launch_progress_phase_label("custom_phase") == "Custom Phase"


def test_launch_progress_route_reports_completed(monkeypatch, api_client, tmp_path):
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path)
    blueprints.record_launch_progress("launch-route", "launch", "failed", "Nope", {"run_id": "run-1"})

    response = api_client.get("/api/v1/blueprints/launch/progress/launch-route")

    assert response.status_code == 200
    assert response.json()["completed"] is True
    assert response.json()["status"] == "failed"
    assert response.json()["run_id"] == "run-1"
    assert response.json()["error"] == "Nope"


def test_launch_progress_websocket_streams_events(monkeypatch, api_client, tmp_path):
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path)
    blueprints.record_launch_progress("launch-ws", "launch", "running", "Accepted", {"run_id": "run-ws"})

    def receive_event(websocket, version):
        for _ in range(6):
            message = websocket.receive_json()
            if message.get("topic") == "launch_progress:launch-ws" and message.get("version") == version:
                return message
        raise AssertionError(f"event version {version} was not received")

    with api_client.websocket_connect("/api/v1/realtime?interval=0.25") as websocket:
        websocket.send_json(
            {
                "requestId": "req-1",
                "action": "subscribe",
                "topic": "launch_progress:launch-ws",
                "after": 0,
            }
        )
        subscribed = websocket.receive_json()
        assert subscribed == {
            "requestId": "req-1",
            "action": "subscribed",
            "topic": "launch_progress:launch-ws",
            "fromVersion": 1,
        }

        first = receive_event(websocket, 1)
        assert first["type"] == "blueprint.launch_progress.launch.running"
        assert first["patch"]["latest"]["phase"] == "launch"
        assert first["patch"]["run_id"] == "run-ws"

        blueprints.record_launch_progress("launch-ws", "model_install", "running", "Installing")
        update = receive_event(websocket, 2)
        assert update["patch"]["latest"]["phase"] == "model_install"

        blueprints.record_launch_progress("launch-ws", "launch", "completed", "Done", {"job_id": "job-ws"})
        terminal = receive_event(websocket, 3)
        assert terminal["patch"]["latest"]["status"] == "completed"
        assert terminal["patch"]["completed"] is True


def test_blueprint_run_returns_submitted_job(monkeypatch, api_client, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    blueprint = {"id": "worker_one", "name": "Worker One", "path": "worker_one"}
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr("mn_api.routes.blueprints.find_blueprint", lambda _config, blueprint_id: (repo_root, blueprint))

    def fake_record(repo, bp, req):
        assert repo == repo_root
        assert bp == blueprint
        assert req.run_id == "run-submitted"
        assert req.progress_id == "progress-submitted"
        blueprints.record_launch_progress(
            req.progress_id,
            "launch",
            "completed",
            "Launch complete.",
            {"run_id": req.run_id, "job_id": "job-submitted"},
        )
        return {
            "job_id": "job-submitted",
            "id": "job-submitted",
            "run_id": req.run_id,
            "status": "pending",
            "progress_id": req.progress_id,
            "progress_url": f"/api/v1/blueprints/launch/progress/{req.progress_id}",
        }

    monkeypatch.setattr("mn_api.routes.blueprints.run_blueprint_record", fake_record)

    response = api_client.post(
        "/api/v1/blueprints/worker_one/runs",
        json={"run_id": "run-submitted", "progress_id": "progress-submitted", "force": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["run_id"] == "run-submitted"
    assert body["progress_id"] == "progress-submitted"
    assert body["progress_url"] == "/api/v1/blueprints/launch/progress/progress-submitted"
    assert body["job_id"] == "job-submitted"


def test_blueprint_run_route_submits_after_real_preflight_with_runtime_environment(monkeypatch, api_client, tmp_path):
    class FakeRuntimeClient:
        def __init__(self):
            self.submissions = []
            self.environment_preparations = []

        def list_jobs(self, _limit=0, _include_terminal=False):
            return json.dumps({"data": []})

        def resolve_service(self, *_args, **_kwargs):
            return json.dumps({"services": []})

        def submit_job(self, manifest_json, payloads, **kwargs):
            self.submissions.append((json.loads(manifest_json), payloads, kwargs))
            return "job-route-real"

        def prepare_runtime_model(self, payload):
            self.environment_preparations.append(payload)
            return json.dumps({
                "status": "ready",
                "runtime_path": "/runtime/shared/blueprint-python-envs/vc-route-real",
                "host_path": "/host/shared/blueprint-python-envs/vc-route-real",
            })

    repo = tmp_path / "catalog"
    bundle = repo / "vc_assistant"
    (bundle / "config").mkdir(parents=True)
    (bundle / "payloads").mkdir()
    (bundle / "payloads" / "payload.txt").write_text("hello", encoding="utf-8")
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "apiVersion": "mn.workflow/v1",
                "kind": "Workflow",
                "workflow": {"workflow_id": "vc_assistant_v1"},
                "nodes": [
                    {
                        "node_id": "worker",
                        "config": {
                            "runner_module": "MirrorNeuron.Runner.HostLocal",
                            "python_environment": {"packages": ["existing-helper==0.1"]},
                            "environment": {},
                        },
                    }
                ],
                "edges": [],
                "skill_dependencies": [
                    {
                        "name": "mirrorneuron-blueprint-support-skill",
                        "source": "gar",
                        "type": "pip",
                        "version": "1.2.8",
                    }
                ],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "config" / "default.json").write_text(
        json.dumps(
            {
                "memory_layer": {
                    "enabled": True,
                    "enabled_env": "MN_CONTEXT_MEMORY_ENABLED",
                    "sdk_import_package": "mn_context_engine_sdk",
                }
            }
        ),
        encoding="utf-8",
    )
    (repo / "index.json").write_text(
        json.dumps([{"id": "vc_assistant", "name": "VC Assistant", "path": "vc_assistant"}]),
        encoding="utf-8",
    )

    fake_runtime = FakeRuntimeClient()
    fake_package = ModuleType("mn_cli")
    fake_package.__path__ = []
    fake_server_cmds = ModuleType("mn_cli.server_cmds")
    observed = {}

    def fake_ensure_context_engine_runtime(*, force=False):
        observed["force"] = force
        observed["path"] = os.environ.get("PATH")
        return {"status": "already_running"}

    fake_server_cmds.ensure_context_engine_runtime = fake_ensure_context_engine_runtime
    monkeypatch.setitem(sys.modules, "mn_cli", fake_package)
    monkeypatch.setitem(sys.modules, "mn_cli.server_cmds", fake_server_cmds)
    monkeypatch.setenv("PATH", "/api/process/bin")
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(
            api_token="",
            request_size_limit_bytes=1024 * 1024,
            blueprint_source="local",
            blueprint_repo="",
            blueprint_local=str(repo),
            active_blueprint_location=str(repo),
        ),
    )
    monkeypatch.setattr(state, "client", fake_runtime)
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr("mn_api.routes.blueprints.validate_blueprint_hardware_requirements", lambda *_args, **_kwargs: {"ok": True, "status": "passed", "issues": [], "errors": []})
    monkeypatch.setattr("mn_api.blueprints.load_model_catalog", lambda: {})
    monkeypatch.setattr(
        "mn_api.blueprints.runtime_path_environment",
        lambda: {
            "PATH": "/runtime/docker/bin:/api/process/bin",
            "PYTHONPATH": f"{tmp_path}/workspace/mn-skills:/runtime/python",
            "MN_WORKSPACE_ROOT": f"{tmp_path}/workspace",
            "MN_SKILLS_ROOT": f"{tmp_path}/workspace/mn-skills",
        },
    )

    response = api_client.post(
        "/api/v1/blueprints/vc_assistant/runs",
        json={
            "run_id": "vc-route-real",
            "progress_id": "progress-route-real",
            "force": True,
            "fake_llm": True,
            "fake_skills": True,
        },
    )
    progress = api_client.get("/api/v1/blueprints/launch/progress/progress-route-real")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["run_id"] == "vc-route-real"
    assert body["job_id"] == "job-route-real"
    assert body["progress_url"] == "/api/v1/blueprints/launch/progress/progress-route-real"
    assert fake_runtime.submissions
    submitted_manifest, payloads, submit_kwargs = fake_runtime.submissions[0]
    assert submitted_manifest["workflow"]["workflow_id"] == "vc_assistant_v1"
    submitted_flow = submitted_manifest.get("flow", submitted_manifest)
    assert submitted_flow["nodes"][0]["node_id"] == "worker"
    worker_config = submitted_flow["nodes"][0]["config"]
    assert worker_config["python_environment"]["path"] == "/runtime/shared/blueprint-python-envs/vc-route-real"
    assert fake_runtime.environment_preparations
    assert fake_runtime.environment_preparations[0]["ensure_hostlocal_python_environment"] is True
    packages = worker_config["python_environment"]["packages"]
    if packages[:5] == [
        "--index-url",
        "https://us-central1-python.pkg.dev/mirrorneuron-public-packages/agent-skills/simple/",
        "--extra-index-url",
        "https://pypi.org/simple",
        "mirrorneuron-blueprint-support-skill==1.2.8",
    ]:
        assert packages[-1] == "existing-helper==0.1"
    else:
        assert packages == ["existing-helper==0.1"]
    worker_env = worker_config["environment"]
    assert worker_env["PYTHONPATH"] == "/runtime/python"
    assert worker_env["MN_BLUEPRINT_FAKE_LLM"] == "1"
    assert worker_env["MN_BLUEPRINT_FAKE_SKILLS"] == "1"
    assert "MN_FAKE_LLM" not in worker_env
    assert "MN_FAKE_SKILLS" not in worker_env
    assert "MN_WORKSPACE_ROOT" not in worker_env
    assert "MN_SKILLS_ROOT" not in worker_env
    assert submitted_flow["edges"] == []
    assert submitted_manifest["metadata"]["mn_cli"]["blueprint_id"] == "vc_assistant"
    assert submitted_manifest["metadata"]["mn_cli"]["blueprint_run_id"] == "vc-route-real"
    assert payloads == {"payload.txt": b"hello"}
    assert submit_kwargs == {"force": True}
    assert observed == {"force": True, "path": "/runtime/docker/bin:/api/process/bin"}
    assert os.environ["PATH"] == "/api/process/bin"
    assert progress.status_code == 200
    assert progress.json()["status"] == "completed"
    assert progress.json()["job_id"] == "job-route-real"


def test_blueprint_launch_returns_progress_session_immediately(monkeypatch, api_client, tmp_path):
    starts = []
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr("mn_api.routes.blueprints.start_async_blueprint_launch", lambda req: starts.append(req))

    response = api_client.post(
        "/api/v1/blueprints/launch/runs",
        json={
            "source": "path",
            "path": str(tmp_path / "local_worker"),
            "run_id": "local-run",
            "progress_id": "local-progress",
            "fake_llm": True,
            "fake_skills": True,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "launching"
    assert body["run_id"] == "local-run"
    assert body["progress_id"] == "local-progress"
    assert body["progress_url"] == "/api/v1/blueprints/launch/progress/local-progress"
    assert body["job_id"] is None
    assert starts[0].source == "path"
    assert starts[0].path == str(tmp_path / "local_worker")
    assert starts[0].run_id == "local-run"
    assert starts[0].progress_id == "local-progress"
    assert starts[0].fake_llm is True
    assert starts[0].fake_skills is True


def test_blueprint_launch_route_background_uses_shared_sdk_submission(monkeypatch, api_client, tmp_path):
    class FakeRuntimeClient:
        def __init__(self):
            self.submissions = []

        def list_jobs(self, _limit=0, _include_terminal=False):
            return json.dumps({"data": []})

        def resolve_service(self, *_args, **_kwargs):
            return json.dumps({"services": []})

        def submit_job(self, manifest_json, payloads, **kwargs):
            self.submissions.append((json.loads(manifest_json), payloads, kwargs))
            return "job-launch-route"

    repo = tmp_path / "catalog"
    bundle = repo / "worker_one"
    (bundle / "payloads").mkdir(parents=True)
    (bundle / "payloads" / "worker.py").write_text("print('ok')\n", encoding="utf-8")
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "graph_id": "worker_graph",
                "nodes": [{"node_id": "worker", "config": {"environment": {}}}],
                "edges": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (repo / "index.json").write_text(
        json.dumps([{"id": "worker_one", "name": "Worker One", "path": "worker_one"}]),
        encoding="utf-8",
    )

    fake_runtime = FakeRuntimeClient()
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(
            api_token="",
            request_size_limit_bytes=1024 * 1024,
            blueprint_source="local",
            blueprint_repo="",
            blueprint_local=str(repo),
            active_blueprint_location=str(repo),
        ),
    )
    monkeypatch.setattr(state, "client", fake_runtime)
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr(
        "mn_api.routes.blueprints.validate_blueprint_hardware_requirements",
        lambda *_args, **_kwargs: {"ok": True, "status": "passed", "issues": [], "errors": []},
    )
    monkeypatch.setattr("mn_api.blueprints.load_model_catalog", lambda: {})

    def run_now(req):
        blueprints.run_blueprint_launch_record(req)
        return SimpleNamespace(name="started")

    monkeypatch.setattr("mn_api.routes.blueprints.start_async_blueprint_launch", run_now)

    response = api_client.post(
        "/api/v1/blueprints/launch/runs",
        json={
            "source": "catalog",
            "blueprint_id": "worker_one",
            "run_id": "launch-route-run",
            "progress_id": "launch-route-progress",
            "force": True,
        },
    )
    progress = api_client.get("/api/v1/blueprints/launch/progress/launch-route-progress")

    assert response.status_code == 202
    assert response.json()["job_id"] is None
    assert fake_runtime.submissions
    submitted_manifest, payloads, submit_kwargs = fake_runtime.submissions[0]
    assert submitted_manifest["run_id"] == "launch-route-run"
    assert submitted_manifest["metadata"]["mn_cli"]["blueprint_id"] == "worker_one"
    assert payloads == {"worker.py": b"print('ok')\n"}
    assert submit_kwargs == {"force": True}
    assert progress.status_code == 200
    assert progress.json()["status"] == "completed"
    assert progress.json()["job_id"] == "job-launch-route"


def test_blueprint_run_generates_progress_id(monkeypatch, api_client, tmp_path):
    requests = []
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr(
        "mn_api.routes.blueprints.find_blueprint",
        lambda _config, blueprint_id: (repo_root, {"id": blueprint_id, "name": "Worker One", "path": "worker_one"}),
    )

    def fake_record(_repo, _bp, req):
        requests.append(req)
        return {"job_id": "job-generated", "id": "job-generated", "run_id": req.run_id, "progress_id": req.progress_id}

    monkeypatch.setattr("mn_api.routes.blueprints.run_blueprint_record", fake_record)

    response = api_client.post("/api/v1/blueprints/worker_one/runs", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"].startswith("worker_one-")
    assert body["progress_id"].startswith(body["run_id"])
    assert blueprints.validate_progress_id(body["progress_id"]) == body["progress_id"]
    assert requests[0].run_id == body["run_id"]
    assert requests[0].progress_id == body["progress_id"]


def test_blueprint_run_progress_polling_reports_success(monkeypatch, api_client, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr(
        "mn_api.routes.blueprints.find_blueprint",
        lambda _config, blueprint_id: (repo_root, {"id": blueprint_id, "name": "Worker One", "path": "worker_one"}),
    )

    def fake_record(_repo, _blueprint, req):
        blueprints.record_launch_progress(
            req.progress_id,
            "launch",
            "completed",
            "Launch complete.",
            {"run_id": req.run_id, "job_id": "job-async"},
        )
        return {
            "job_id": "job-async",
            "id": "job-async",
            "run_id": req.run_id,
            "progress_id": req.progress_id,
            "progress_url": f"/api/v1/blueprints/launch/progress/{req.progress_id}",
        }

    monkeypatch.setattr("mn_api.routes.blueprints.run_blueprint_record", fake_record)

    start = api_client.post("/api/v1/blueprints/worker_one/runs", json={"run_id": "run-ok"})
    progress = api_client.get(start.json()["progress_url"])

    assert start.status_code == 200
    assert progress.status_code == 200
    body = progress.json()
    assert body["completed"] is True
    assert body["status"] == "completed"
    assert body["run_id"] == "run-ok"
    assert body["job_id"] == "job-async"


def test_blueprint_run_progress_polling_reports_background_error(monkeypatch, api_client, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path / "progress")
    monkeypatch.setattr(
        "mn_api.routes.blueprints.find_blueprint",
        lambda _config, blueprint_id: (repo_root, {"id": blueprint_id, "name": "Worker One", "path": "worker_one"}),
    )

    def fake_record(_repo, _blueprint, req):
        blueprints.record_launch_progress(
            req.progress_id,
            "launch",
            "failed",
            "Runtime model install failed.",
            {"run_id": req.run_id},
            severity="error",
        )
        return {
            "job_id": None,
            "run_id": req.run_id,
            "progress_id": req.progress_id,
            "progress_url": f"/api/v1/blueprints/launch/progress/{req.progress_id}",
        }

    monkeypatch.setattr("mn_api.routes.blueprints.run_blueprint_record", fake_record)

    start = api_client.post("/api/v1/blueprints/worker_one/runs", json={"run_id": "run-error"})
    progress = api_client.get(start.json()["progress_url"])

    assert start.status_code == 200
    assert progress.status_code == 200
    body = progress.json()
    assert body["completed"] is True
    assert body["status"] == "failed"
    assert body["run_id"] == "run-error"
    assert body["job_id"] is None
    assert body["error"] == "Runtime model install failed."


def test_resolve_launch_source_validates_required_fields(monkeypatch):
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "catalog", "blueprint_id": None})())
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "path", "path": None})())
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "bundle", "bundle_path": None})())
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "other"})())

    monkeypatch.setattr("mn_api.routes.blueprints.load_uploaded_bundle", lambda bundle_path, upload_root: (json.dumps({"graph_id": "g"}), {}))
    resolved = blueprints.resolve_launch_source(type("Req", (), {"source": "bundle", "bundle_path": "/tmp/uploaded"})())

    assert resolved["source"] == "bundle"
    assert resolved["blueprint"]["id"] == "g"
