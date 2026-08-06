import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mn_api import state
from mn_api.main import app


def test_unfinished_jobs_route_marks_recovery_state(monkeypatch):
    class FakeClient:
        def list_jobs(self, limit, include_terminal):
            assert limit == 500
            assert include_terminal is False
            return json.dumps(
                {
                    "data": [
                        {
                            "job_id": "job-1",
                            "status": "paused",
                            "recovery": {"status": "needs_resume", "requires_review": True},
                        }
                    ]
                }
            )

    monkeypatch.setattr(state, "client", FakeClient())

    response = TestClient(app).get("/api/v2/jobs/unfinished")

    assert response.status_code == 200
    assert response.json()["data"][0]["recovery_status"] == "needs_resume"
    assert response.json()["data"][0]["recovery_requires_review"] is True


def test_nodes_route_strips_restart_history(monkeypatch):
    class FakeClient:
        def get_system_summary(self):
            return json.dumps(
                {
                    "nodes": [
                        {
                            "name": "mirror_neuron@local",
                            "restart_history": ["hidden"],
                            "restart_reason": "hidden",
                            "status": "running",
                        }
                    ]
                }
            )

    monkeypatch.setattr(state, "client", FakeClient())

    response = TestClient(app).get("/api/v2/nodes")

    assert response.status_code == 200
    node = response.json()["nodes"][0]
    assert node == {"name": "mirror_neuron@local", "status": "running"}


def test_runtime_health_and_doctor_routes(monkeypatch):
    monkeypatch.setattr(
        "mn_api.routes.system.collect_runtime_status",
        lambda **_kwargs: {
            "version": 2,
            "overall": "passing",
            "checked_at": "2026-07-06T00:00:00Z",
            "runtime": {},
            "endpoints": {},
            "components": [{"name": "core_grpc", "status": "passing"}],
            "nodes": {},
            "jobs": {},
            "shared_storage": {},
        },
    )
    monkeypatch.setattr("mn_api.routes.system.docker_status", lambda: {"ok": True, "available": True})
    monkeypatch.setattr("mn_api.routes.system.litellm_gateway_health", lambda timeout=3.0: {"ok": False})

    client = TestClient(app)

    health = client.get("/api/v2/runtime/health")
    doctor = client.get("/api/v2/runtime/doctor")

    assert health.status_code == 200
    assert health.json()["overall"] == "passing"
    assert doctor.status_code == 200
    assert doctor.json()["foundation"]["docker_model_runner"]["status"] == "passing"
    assert doctor.json()["foundation"]["litellm_gateway"]["status"] == "warning"


def test_resource_ports_route(monkeypatch):
    monkeypatch.setattr(
        "mn_api.routes.system.native_service_ports",
        lambda: [{"name": "api", "host": "127.0.0.1", "port": "54001"}],
    )

    response = TestClient(app).get("/api/v2/resource/ports")

    assert response.status_code == 200
    assert response.json()["ports"][0]["name"] == "api"


def test_service_check_route_uses_bundle_and_sdk_validation(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"graph_id": "g", "nodes": []}), encoding="utf-8")
    observed = {}

    def fake_validation(bundle_dir, manifest, **kwargs):
        observed["bundle_dir"] = bundle_dir
        observed["manifest"] = manifest
        observed["kwargs"] = kwargs
        return {"ok": True, "checks": []}

    monkeypatch.setattr("mn_api.routes.services.run_service_validation", fake_validation)

    response = TestClient(app).post("/api/v2/services:check", json={"path": str(bundle)})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert observed["bundle_dir"] == bundle.resolve()
    assert observed["manifest"]["graph_id"] == "g"


def test_model_remote_and_proxy_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("MN_MODEL_REMOTES_PATH", str(tmp_path / "remotes.json"))
    monkeypatch.setenv("MN_MODEL_PROXIES_PATH", str(tmp_path / "proxies.json"))
    client = TestClient(app)

    added = client.post(
        "/api/v2/models/remotes",
        json={"model": "ai/qwen3-coder", "base_url": "http://192.168.4.173:12434/v1", "name": "spark"},
    )
    listed = client.get("/api/v2/models/remotes")
    removed = client.delete("/api/v2/models/remotes/spark")
    proxied = client.post("/api/v2/models/proxies", json={"model_id": "openai/gpt-4.1", "base_url": "http://127.0.0.1:4000/v1"})

    assert added.status_code == 200
    assert added.json()["remote"]["name"] == "spark"
    assert listed.status_code == 200
    assert listed.json()["remotes"][0]["name"] == "spark"
    assert removed.status_code == 200
    assert removed.json()["removed"]["name"] == "spark"
    assert proxied.status_code == 200
    assert proxied.json()["proxy"]["id"] == "openai/gpt-4.1"


def test_run_list_export_and_compare_routes(tmp_path):
    runs_root = Path(os.environ["MN_HOME"]) / "runs"
    _write_run(runs_root, "run-a", blueprint_id="bp", status="completed", artifact={"score": 1})
    _write_run(runs_root, "run-b", blueprint_id="bp", status="failed", artifact={"score": 2})
    client = TestClient(app)

    listed = client.get("/api/v2/runtime-runs?blueprint_id=bp")
    exported_json = client.get("/api/v2/runtime-runs/run-a/export")
    exported_markdown = client.get("/api/v2/runtime-runs/run-a/export?format=markdown")
    compared = client.post("/api/v2/runtime-runs:compare", json={"run_a": "run-a", "run_b": "run-b"})

    assert listed.status_code == 200
    assert [row["run_id"] for row in listed.json()["data"]] == ["run-b", "run-a"]
    assert exported_json.status_code == 200
    assert exported_json.json()["final_artifact"]["score"] == 1
    assert exported_markdown.status_code == 200
    assert "Blueprint Run run-a" in exported_markdown.text
    assert compared.status_code == 200
    assert compared.json()["artifact_diff"]["score"] == {"run-a": 1, "run-b": 2}


def test_job_workflow_progress_websocket(monkeypatch):
    class FakeClient:
        def stream_events(self, *_args, **_kwargs):
            yield json.dumps({"type": "job_running"})
            yield json.dumps({"type": "job_completed"})

    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(
        "mn_api.routes.jobs._workflow_progress_snapshot_for_job",
        lambda job_id: {"job_id": job_id, "status": "running"},
    )

    with TestClient(app).websocket_connect("/api/v2/jobs/job-1/workflow-progress/ws") as websocket:
        assert websocket.receive_json() == {"event": "snapshot", "data": {"job_id": "job-1", "status": "running"}}
        assert websocket.receive_json()["event"] == "event"
        assert websocket.receive_json()["event"] == "snapshot"


def test_run_websockets(monkeypatch, tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    monkeypatch.setattr("mn_api.routes.runs._ensure_run_exists", lambda _run_id: run_dir)
    monkeypatch.setattr("mn_api.routes.runs.list_runs", lambda **_kwargs: [{"run_id": "run-1"}])
    monkeypatch.setattr(
        "mn_api.routes.runs._observability_tools",
        lambda: {
            "read_run_stream_records": lambda *_args, **_kwargs: [
                {"id": "event-1", "channel": "events", "ts": "2026-07-06T00:00:00Z", "type": "job_running"}
            ],
            "read_run_resources": lambda *_args, **_kwargs: {"run_id": "run-1", "sample_count": 1},
        },
    )

    client = TestClient(app)
    with client.websocket_connect("/api/v2/runtime-runs/ws") as websocket:
        assert websocket.receive_json() == {"event": "runs", "data": [{"run_id": "run-1"}]}
    with client.websocket_connect("/api/v2/runtime-runs/run-1/stream/ws") as websocket:
        assert websocket.receive_json()["id"] == "event-1"
    with client.websocket_connect("/api/v2/runtime-runs/run-1/resources/ws") as websocket:
        assert websocket.receive_json() == {"event": "resources", "data": {"run_id": "run-1", "sample_count": 1}}


def test_websocket_auth_accepts_query_token(monkeypatch):
    original_config = state.config
    monkeypatch.setattr(
        state,
        "config",
        SimpleNamespace(api_token="secret", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]),
    )
    monkeypatch.setattr("mn_api.routes.runs.list_runs", lambda **_kwargs: [])

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v2/runtime-runs/ws"):
            pass
    with client.websocket_connect("/api/v2/runtime-runs/ws?token=secret") as websocket:
        assert websocket.receive_json() == {"event": "runs", "data": []}

    monkeypatch.setattr(state, "config", original_config)


def _write_run(runs_root: Path, run_id: str, *, blueprint_id: str, status: str, artifact: dict):
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "blueprint_id": blueprint_id,
                "status": status,
                "started_at": f"2026-07-06T00:00:0{1 if run_id.endswith('a') else 2}Z",
                "run_dir": str(run_dir),
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "final_artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(json.dumps({"ts": "2026-07-06T00:00:00Z", "type": status}) + "\n", encoding="utf-8")
