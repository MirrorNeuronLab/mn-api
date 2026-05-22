import unittest
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from types import SimpleNamespace
from mn_api.config import ApiConfig
from mn_api import state
from mn_api.main import app
from unittest.mock import patch
import grpc

class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _write_blueprint_repo(self, repo: Path):
        blueprint_dir = repo / "worker_one"
        payloads_dir = blueprint_dir / "payloads"
        payloads_dir.mkdir(parents=True)
        (payloads_dir / "payload.txt").write_text("hello")
        (blueprint_dir / "manifest.json").write_text(json.dumps({
            "graph_id": "worker_one_graph",
            "nodes": [],
            "edges": [],
            "metadata": {},
        }))
        (repo / "index.json").write_text(json.dumps([
            {
                "id": "worker_one",
                "name": "Worker One",
                "path": "worker_one",
                "category": "Business",
                "description": "A test worker.",
                "product": {
                    "one_line": "A normalized test worker.",
                    "agent_role": "Test operator.",
                    "target_users": "Testers",
                    "output": "Test output",
                    "runtime_features": ["testing"],
                },
            }
        ]))
        (repo / "category.json").write_text(json.dumps({
            "categories": [
                {"name": "Business", "slug": "business"},
                {"name": "Finance", "slug": "finance"},
            ]
        }))

    def _set_blueprint_config(self, repo: Path, token: str = ""):
        original = state.config
        state.config = SimpleNamespace(
            api_token=token,
            request_size_limit_bytes=1024 * 1024,
            blueprint_repo=str(repo),
        )
        return original

    def _restore_config(self, original):
        state.config = original

    def test_health(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "auth": "disabled"})

    def test_config_uses_grpc_auth_token(self):
        with patch.dict(os.environ, {"MN_GRPC_AUTH_TOKEN": "auth-secret"}, clear=False):
            config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-secret")

    def test_config_uses_grpc_admin_token(self):
        with patch.dict(os.environ, {"MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN": "admin-secret"}, clear=False):
            config = ApiConfig.from_env()

        self.assertEqual(config.grpc_admin_token, "admin-secret")

    def test_config_uses_local_grpc_token_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp) / ".mirror_neuron"
            token_dir.mkdir()
            (token_dir / "grpc_auth.token").write_text("auth-from-file\n")
            (token_dir / "grpc_admin.token").write_text("admin-from-file\n")

            with patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "MN_GRPC_AUTH_TOKEN": "",
                    "MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN": "",
                },
                clear=False,
            ):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-from-file")
        self.assertEqual(config.grpc_admin_token, "admin-from-file")

    def test_auth_required_when_token_configured(self):
        original = state.config
        state.config = SimpleNamespace(api_token="secret", request_size_limit_bytes=1024 * 1024)
        try:
            response = self.client.get("/api/v1/system/summary")
            self.assertEqual(response.status_code, 401)
            response = self.client.get(
                "/api/v1/system/summary",
                headers={"Authorization": "Bearer secret"},
            )
            self.assertIn(response.status_code, (200, 500))
        finally:
            state.config = original

    def test_request_size_limit(self):
        original = state.config
        state.config = SimpleNamespace(api_token="", request_size_limit_bytes=10)
        try:
            response = self.client.post(
                "/api/v1/jobs",
                headers={"content-length": "11"},
                json={"manifest_json": "{}", "payloads": {}},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"], "request_too_large")
        finally:
            state.config = original

    def test_invalid_content_length_is_rejected(self):
        response = self.client.post(
            "/api/v1/jobs",
            headers={"content-length": "not-a-number"},
            content=b"{}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_content_length"})

    @patch('mn_api.state.client')
    def test_list_jobs_success(self, mock_client):
        mock_client.list_jobs.return_value = '{"data": [{"job_id": "job-1"}]}'
        response = self.client.get("/api/v1/jobs?limit=5&include_terminal=false")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": [{"job_id": "job-1"}]})
        mock_client.list_jobs.assert_called_once_with(5, False)

    @patch('mn_api.state.client')
    def test_cleanup_jobs_success(self, mock_client):
        mock_client.clear_jobs.return_value = 3
        response = self.client.post("/api/v1/jobs:cleanup")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"cleared_count": 3})
        mock_client.clear_jobs.assert_called_once()

    @patch('mn_api.state.client')
    def test_get_system_summary_success(self, mock_client):
        mock_client.get_system_summary.return_value = '{"nodes": [], "jobs": []}'
        response = self.client.get("/api/v1/system/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"nodes": [], "jobs": []})

    @patch('mn_api.state.client')
    def test_get_resource_success(self, mock_client):
        mock_client.get_resource.return_value = '{"totals": {"cpu_cores": 8}, "limits": {"cpu": 100, "gpu": 100, "memory": 100}}'
        response = self.client.get("/api/v1/resource")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totals"]["cpu_cores"], 8)
        mock_client.get_resource.assert_called_once()

    @patch('mn_api.state.client')
    def test_set_resource_success(self, mock_client):
        mock_client.set_resource.return_value = '{"limits": {"cpu": 50, "gpu": 75, "memory": 100}}'
        response = self.client.put(
            "/api/v1/resource",
            json={"cpu": 50, "gpu": 75, "memory": 100},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limits"]["gpu"], 75)
        mock_client.set_resource.assert_called_once_with({"cpu": 50, "gpu": 75, "memory": 100})

    @patch('mn_api.state.client')
    def test_submit_job_success(self, mock_client):
        mock_client.submit_job.return_value = "job-123"
        response = self.client.post(
            "/api/v1/jobs",
            json={"manifest_json": '{"graph_id": "g"}', "payloads": {"a.txt": "hello"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "job-123", "status": "pending"})
        mock_client.submit_job.assert_called_once_with('{"graph_id": "g"}', {"a.txt": b"hello"})

    @patch('mn_api.state.client')
    def test_upload_bundle_and_submit_by_bundle_path(self, mock_client):
        mock_client.submit_job.return_value = "job-zip"
        archive = io.BytesIO()
        manifest = {"graph_id": "zip_graph", "nodes": [], "edges": []}

        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("manifest.json", json.dumps(manifest))
            zip_file.writestr("payloads/a.txt", "hello")
        archive.seek(0)

        upload_response = self.client.post(
            "/api/v1/bundles/upload",
            files={"bundle": ("bundle.zip", archive, "application/zip")},
        )
        self.assertEqual(upload_response.status_code, 200)
        bundle_path = upload_response.json()["bundle_path"]
        self.assertEqual(upload_response.json()["manifest"]["graph_id"], "zip_graph")

        submit_response = self.client.post(
            "/api/v1/jobs",
            json={"_bundle_path": bundle_path},
        )
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(submit_response.json(), {"id": "job-zip", "status": "pending"})
        mock_client.submit_job.assert_called_once_with(
            json.dumps(manifest),
            {"a.txt": b"hello"},
        )

    def test_upload_bundle_accepts_single_nested_bundle_root(self):
        archive = io.BytesIO()
        manifest = {"graph_id": "nested_zip_graph", "nodes": [], "edges": []}

        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("bundle-root/manifest.json", json.dumps(manifest))
            zip_file.writestr("bundle-root/payloads/a.txt", "hello")
        archive.seek(0)

        response = self.client.post(
            "/api/v1/bundles/upload",
            files={"bundle": ("bundle.zip", archive, "application/zip")},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["manifest"]["graph_id"], "nested_zip_graph")
        self.assertTrue(body["bundle_path"].endswith("bundle-root"))

    def test_upload_bundle_rejects_unsafe_paths(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr("../manifest.json", "{}")
        archive.seek(0)

        response = self.client.post(
            "/api/v1/bundles/upload",
            files={"bundle": ("bundle.zip", archive, "application/zip")},
        )
        self.assertEqual(response.status_code, 400)

    def test_get_run_ui_reads_saved_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "blueprint-run-1"
            run_dir.mkdir()
            (run_dir / "ui.json").write_text(json.dumps({
                "adapter": "gradio",
                "title": "Blueprint Run",
                "refresh_seconds": 1.5,
                "components": [{"type": "events"}],
            }))
            (run_dir / "web_ui.json").write_text(json.dumps({
                "adapter": "gradio",
                "url": "http://localhost:7860/runs/blueprint-run-1/ui",
            }))
            (run_dir / "job.json").write_text(json.dumps({"job_id": "job-1"}))
            (run_dir / "events.jsonl").write_text(
                "\n".join([
                    json.dumps({"type": "first"}),
                    "not-json",
                    json.dumps({"type": "last", "payload": {"ok": True}}),
                ])
            )

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/runs/blueprint-run-1/ui?limit=2")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["ui"]["adapter"], "gradio")
        self.assertEqual(body["web_ui"]["url"], "http://localhost:7860/runs/blueprint-run-1/ui")
        self.assertEqual(body["job"]["job_id"], "job-1")
        self.assertEqual([event["type"] for event in body["events"]], ["unparseable_event", "last"])

    def test_get_run_ui_rejects_invalid_run_id(self):
        response = self.client.get("/api/v1/runs/bad$id/ui")
        self.assertEqual(response.status_code, 400)

    def test_run_observability_endpoints_read_shared_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "observe-run"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(json.dumps({
                "run_id": "observe-run",
                "blueprint_id": "general_human_in_the_loop_workflow",
                "status": "running",
            }))
            (run_dir / "events.jsonl").write_text(
                json.dumps({
                    "ts": "2026-05-22T12:00:00Z",
                    "run_id": "observe-run",
                    "blueprint_id": "general_human_in_the_loop_workflow",
                    "type": "run_started",
                    "payload": {},
                })
                + "\n"
            )
            (run_dir / "logs.jsonl").write_text(
                json.dumps({
                    "ts": "2026-05-22T12:00:01Z",
                    "run_id": "observe-run",
                    "blueprint_id": "general_human_in_the_loop_workflow",
                    "level": "WARN",
                    "component": "worker",
                    "message": "needs attention",
                })
                + "\n"
            )
            (run_dir / "human.jsonl").write_text(
                json.dumps({
                    "ts": "2026-05-22T12:00:02Z",
                    "run_id": "observe-run",
                    "blueprint_id": "general_human_in_the_loop_workflow",
                    "channel": "human",
                    "type": "human_input_requested",
                    "payload": {"request_id": "hitl-1", "prompt": "Approve?"},
                })
                + "\n"
            )
            (run_dir / "resources.jsonl").write_text(
                json.dumps({
                    "ts": "2026-05-22T12:00:03Z",
                    "run_id": "observe-run",
                    "blueprint_id": "general_human_in_the_loop_workflow",
                    "component": "worker",
                    "cpu_pct": 12.5,
                    "memory_rss_mb": 256,
                    "gpu": [],
                    "llm": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "calls": 1, "estimated": False},
                })
                + "\n"
            )

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                logs = self.client.get("/api/v1/runs/observe-run/logs?level=INFO")
                human = self.client.get("/api/v1/runs/observe-run/human?status=pending")
                response = self.client.post(
                    "/api/v1/runs/observe-run/human/hitl-1/response",
                    json={"decision": "approve", "notes": "ok"},
                )
                resources = self.client.get("/api/v1/runs/observe-run/resources?window=24000h&bucket=1h")

        self.assertEqual(logs.status_code, 200)
        self.assertEqual(logs.json()["data"][0]["message"], "needs attention")
        self.assertEqual(human.status_code, 200)
        self.assertEqual(human.json()["data"][0]["payload"]["request_id"], "hitl-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"]["approved"], True)
        self.assertEqual(resources.status_code, 200)
        self.assertEqual(resources.json()["sample_count"], 1)

    @patch('mn_api.state.client')
    def test_submit_by_unknown_bundle_path_is_rejected_before_sdk_call(self, mock_client):
        response = self.client.post(
            "/api/v1/jobs",
            json={"_bundle_path": str(Path(tempfile.gettempdir()) / "outside-bundle")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "unknown uploaded bundle")
        mock_client.submit_job.assert_not_called()

    @patch('mn_api.state.client')
    def test_cancel_job_success(self, mock_client):
        mock_client.cancel_job.return_value = "cancelled"
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "cancelled", "job_id": "test_job_123"})

    @patch('mn_api.state.client')
    def test_cancel_job_grpc_error(self, mock_client):
        class MockRpcError(Exception):
            def details(self):
                return "job test_job_123 was not found"
                
        mock_client.cancel_job.side_effect = MockRpcError()
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "job test_job_123 was not found"})

    @patch('mn_api.state.client')
    def test_cancel_job_generic_error(self, mock_client):
        mock_client.cancel_job.side_effect = Exception("Some generic error")
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "Some generic error"})

    @patch('mn_api.state.client')
    def test_submit_job_resource_overloaded(self, mock_client):
        class ResourceError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.RESOURCE_EXHAUSTED

            def details(self):
                return "resource_overloaded: memory=0.99 threshold=0.95"

        mock_client.submit_job.side_effect = ResourceError()
        response = self.client.post(
            "/api/v1/jobs",
            json={"manifest_json": '{"graph_id": "g"}', "payloads": {}},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "resource_overloaded")

    @patch('mn_api.state.client')
    def test_submit_job_input_validation_problem_details(self, mock_client):
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "manifest_json": json.dumps({
                    "graph_id": "g",
                    "input_validation": {
                        "rules": [
                            {
                                "name": "endpoint_url",
                                "type": "pattern",
                                "path": "endpoint",
                                "pattern": "^https://",
                            }
                        ]
                    },
                }),
                "payloads": {},
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.json()["error"], "input_validation_failed")
        self.assertEqual(response.json()["errors"][0]["location"]["path"], "endpoint")
        mock_client.submit_job.assert_not_called()

    @patch('mn_api.state.client')
    def test_submit_job_requirements_error_problem_details(self, mock_client):
        report = {
            "version": "validation.report/v1",
            "ok": False,
            "status": "failed",
            "error_count": 1,
            "errors": ["memory requires at least 32 GB, found 16"],
            "issues": [
                {
                    "code": "requirements.memory_insufficient",
                    "message": "memory requires at least 32 GB, found 16",
                    "help": "Run this blueprint on a larger machine.",
                    "severity": "error",
                    "location": {"source": "requirements", "path": "memory", "pointer": "/requirements/memory"},
                    "expected": {"resource": "memory", "minimum": 32, "unit": "GB"},
                    "actual": {"resource": "memory", "available": 16, "unit": "GB"},
                }
            ],
            "results": [],
        }

        class RequirementsError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.FAILED_PRECONDITION

            def details(self):
                return "requirements_not_met: " + json.dumps(report)

        mock_client.submit_job.side_effect = RequirementsError()
        response = self.client.post(
            "/api/v1/jobs",
            json={"manifest_json": '{"graph_id": "g"}', "payloads": {}},
        )

        self.assertEqual(response.status_code, 412)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.json()["error"], "requirements_not_met")
        self.assertEqual(response.json()["errors"][0]["code"], "requirements.memory_insufficient")
        self.assertEqual(response.json()["errors"][0]["location"]["path"], "memory")

    @patch('mn_api.state.client')
    def test_get_job_events_success(self, mock_client):
        mock_client.stream_events.return_value = ['{"id": "e1"}', '{"id": "e2"}']
        response = self.client.get("/api/v1/jobs/test_job_123/events")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": [{"id": "e1"}, {"id": "e2"}]})

    @patch('mn_api.state.client')
    def test_get_job_agent_graph_success(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {"job_id": "job-1", "graph_id": "graph-1", "status": "running"},
            "agents": [
                {"agent_id": "planner", "agent_type": "router", "status": "ready"},
                {"agent_id": "worker", "agent_type": "executor", "status": "running"},
            ],
        })
        mock_client.stream_events.return_value = [
            json.dumps({
                "type": "agent_message_received",
                "timestamp": "2026-04-29T12:00:00Z",
                "payload": {"from": "planner", "to": "worker", "type": "task"},
            }),
            json.dumps({
                "type": "agent_message_received",
                "timestamp": "2026-04-29T12:00:01Z",
                "payload": {"from": "planner", "to": "worker", "type": "task"},
            }),
        ]

        response = self.client.get("/api/v1/jobs/job-1/agent-graph")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["job_id"], "job-1")
        self.assertEqual(body["stats"]["agent_count"], 2)
        self.assertEqual(body["stats"]["message_count"], 2)
        self.assertEqual(body["edges"][0]["source"], "planner")
        self.assertEqual(body["edges"][0]["target"], "worker")
        self.assertEqual(body["edges"][0]["message_type"], "task")
        self.assertEqual(body["edges"][0]["count"], 2)

    @patch('mn_api.state.client')
    def test_get_job_agent_graph_includes_manifest_edges(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest_path.write_text(json.dumps({
                "graph_id": "graph-1",
                "nodes": [
                    {"node_id": "ingress", "agent_type": "router", "type": "generic"},
                    {"node_id": "source", "agent_type": "executor", "type": "stream"},
                    {"node_id": "sink", "agent_type": "executor", "type": "stream"},
                ],
                "edges": [
                    {
                        "edge_id": "ingress_to_source",
                        "from_node": "ingress",
                        "to_node": "source",
                        "message_type": "stream_start",
                    },
                    {
                        "edge_id": "source_to_sink",
                        "from_node": "source",
                        "to_node": "sink",
                        "message_type": "telemetry_event",
                    },
                ],
            }))
            mock_client.get_job.return_value = json.dumps({
                "job": {
                    "job_id": "job-1",
                    "graph_id": "graph-1",
                    "status": "running",
                    "manifest_ref": {"manifest_path": str(manifest_path)},
                },
                "agents": [
                    {"agent_id": "ingress", "agent_type": "router", "status": "ready"},
                    {"agent_id": "source", "agent_type": "executor", "status": "running"},
                    {"agent_id": "sink", "agent_type": "executor", "status": "running"},
                ],
            })
            mock_client.stream_events.return_value = []

            response = self.client.get("/api/v1/jobs/job-1/agent-graph")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stats"]["agent_count"], 3)
        self.assertEqual(body["stats"]["edge_count"], 2)
        self.assertEqual(body["stats"]["message_count"], 0)
        self.assertEqual(
            {(edge["source"], edge["target"], edge["message_type"], edge["source_event"]) for edge in body["edges"]},
            {
                ("ingress", "source", "stream_start", "manifest"),
                ("source", "sink", "telemetry_event", "manifest"),
            },
        )

    @patch('mn_api.state.client')
    def test_get_job_agent_graph_includes_persisted_topology_edges(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {
                "job_id": "job-1",
                "graph_id": "graph-1",
                "status": "running",
                "topology": {
                    "nodes": [
                        {"node_id": "source", "agent_type": "executor", "type": "stream"},
                        {"node_id": "sink", "agent_type": "executor", "type": "stream"},
                    ],
                    "edges": [
                        {
                            "edge_id": "source_to_sink",
                            "from_node": "source",
                            "to_node": "sink",
                            "message_type": "telemetry_event",
                        },
                    ],
                },
            },
            "agents": [],
        })
        mock_client.stream_events.return_value = []

        response = self.client.get("/api/v1/jobs/job-1/agent-graph")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stats"]["agent_count"], 2)
        self.assertEqual(body["stats"]["edge_count"], 1)
        self.assertEqual(body["edges"][0]["source"], "source")
        self.assertEqual(body["edges"][0]["target"], "sink")
        self.assertEqual(body["edges"][0]["message_type"], "telemetry_event")
        self.assertEqual(body["edges"][0]["count"], 0)

    @patch('mn_api.state.client')
    def test_get_job_agent_graph_derives_edges_from_message_envelopes(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {"job_id": "job-1", "graph_id": "graph-1", "status": "running"},
            "agents": [{"agent_id": "sink", "agent_type": "executor", "status": "running"}],
        })
        mock_client.stream_events.return_value = [
            json.dumps({
                "type": "agent_message_received",
                "timestamp": "2026-04-29T12:00:00Z",
                "agent_id": "sink",
                "message": {
                    "envelope": {
                        "from": "external-source",
                        "to": "sink",
                        "type": "replayed_task",
                    }
                },
            })
        ]

        response = self.client.get("/api/v1/jobs/job-1/agent-graph")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stats"]["agent_count"], 2)
        self.assertEqual(body["stats"]["message_count"], 1)
        self.assertEqual(body["edges"][0]["source"], "external-source")
        self.assertEqual(body["edges"][0]["target"], "sink")
        self.assertEqual(body["edges"][0]["message_type"], "replayed_task")

    @patch('mn_api.state.client')
    def test_get_job_agent_graph_includes_outbound_edge_metadata(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {"job_id": "job-1", "graph_id": "graph-1", "status": "running"},
            "agents": [
                {
                    "agent_id": "planner",
                    "agent_type": "router",
                    "status": "running",
                    "metadata": {"outbound_edges": ["worker"]},
                }
            ],
        })
        mock_client.stream_events.return_value = []

        response = self.client.get("/api/v1/jobs/job-1/agent-graph")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stats"]["agent_count"], 2)
        self.assertEqual(body["stats"]["edge_count"], 1)
        self.assertEqual(body["edges"][0]["source_event"], "outbound_edges")
        self.assertEqual(body["edges"][0]["source"], "planner")
        self.assertEqual(body["edges"][0]["target"], "worker")

    @patch('mn_api.state.client')
    def test_get_job_dead_letters_success(self, mock_client):
        mock_client.stream_events.return_value = [
            '{"type": "agent_started"}',
            '{"type": "dead_letter", "agent_id": "slow", "reason": "queue full"}',
        ]
        response = self.client.get("/api/v1/jobs/test_job_123/dead-letters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["reason"], "queue full")

    @patch('mn_api.state.client')
    def test_metrics_success(self, mock_client):
        mock_client.get_system_summary.return_value = '{"nodes": ["n1"], "jobs": [{"status": "running"}, {"status": "failed"}]}'
        response = self.client.get("/api/v1/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["jobs"]["by_status"], {"running": 1, "failed": 1})

    @patch('mn_api.state.client')
    def test_pause_and_resume_job_success(self, mock_client):
        mock_client.pause_job.return_value = "paused"
        pause_response = self.client.post("/api/v1/jobs/test_job_123/pause")
        self.assertEqual(pause_response.status_code, 200)
        self.assertEqual(pause_response.json(), {"status": "paused", "job_id": "test_job_123"})

        mock_client.resume_job.return_value = "running"
        resume_response = self.client.post("/api/v1/jobs/test_job_123/resume")
        self.assertEqual(resume_response.status_code, 200)
        self.assertEqual(resume_response.json(), {"status": "running", "job_id": "test_job_123"})

    def test_blueprint_list_detail_and_install_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                list_response = self.client.get("/api/v1/blueprints")
                detail_response = self.client.get("/api/v1/blueprints/worker_one")
                install_response = self.client.post("/api/v1/blueprints/worker_one/install")
            finally:
                self._restore_config(original)

        self.assertEqual(list_response.status_code, 200)
        body = list_response.json()
        self.assertEqual(body["blueprints"][0]["id"], "worker_one")
        self.assertEqual(body["blueprints"][0]["category"], "Business")
        self.assertEqual(body["blueprints"][0]["category_slug"], "business")
        self.assertEqual(body["categories"][0], {"name": "Business", "slug": "business", "count": 1})
        self.assertEqual(body["blueprints"][0]["agent_role"], "Test operator.")
        self.assertEqual(body["blueprints"][0]["pricing"]["model"], "free")
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["blueprint"]["name"], "Worker One")
        self.assertEqual(install_response.status_code, 200)
        self.assertTrue(install_response.json()["installed"])

    def test_blueprint_list_filters_by_category(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            index = json.loads((repo / "index.json").read_text())
            index.append({
                "id": "worker_two",
                "name": "Worker Two",
                "path": "worker_two",
                "category": "Finance",
                "description": "A finance worker.",
            })
            (repo / "index.json").write_text(json.dumps(index))
            original = self._set_blueprint_config(repo)
            try:
                finance_response = self.client.get("/api/v1/blueprints?category=finance")
                business_response = self.client.get("/api/v1/blueprints?category=Business")
            finally:
                self._restore_config(original)

        self.assertEqual(finance_response.status_code, 200)
        self.assertEqual([bp["id"] for bp in finance_response.json()["blueprints"]], ["worker_two"])
        self.assertEqual(
            finance_response.json()["categories"],
            [
                {"name": "Business", "slug": "business", "count": 1},
                {"name": "Finance", "slug": "finance", "count": 1},
            ],
        )
        self.assertEqual(business_response.status_code, 200)
        self.assertEqual([bp["id"] for bp in business_response.json()["blueprints"]], ["worker_one"])

    @patch('mn_api.state.client')
    def test_blueprint_run_returns_job_and_run_ids(self, mock_client):
        mock_client.submit_job.return_value = "job-blueprint-1"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text(json.dumps({
                "graph_id": "worker_one_graph",
                "nodes": [{"node_id": "worker", "config": {"environment": {}}}],
                "edges": [],
                "metadata": {},
            }))
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "identity": {"run_id": "stale-run"},
                "outputs": {"run_root": str(repo / "blueprints" / "worker_one" / "runs")},
                "manifest_config_bindings": [
                    {
                        "config_path": "identity.run_id",
                        "manifest_path": "nodes.*.config.environment.MN_RUN_ID",
                    },
                    {
                        "config_path": "outputs.run_root",
                        "manifest_path": "nodes.*.config.environment.MN_RUNS_ROOT",
                    },
                ],
            }))
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    response = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-123"},
                    )
                    job_mapping = json.loads((runs_root / "run-123" / "job.json").read_text())
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "job-blueprint-1")
        self.assertEqual(response.json()["run_id"], "run-123")
        self.assertEqual(response.json()["status"], "pending")
        manifest_json, payloads = mock_client.submit_job.call_args.args
        manifest = json.loads(manifest_json)
        self.assertEqual(manifest["run_id"], "run-123")
        self.assertEqual(manifest["metadata"]["blueprint_id"], "worker_one")
        self.assertEqual(manifest["metadata"]["blueprint_run_id"], "run-123")
        env = manifest["nodes"][0]["config"]["environment"]
        self.assertEqual(env["MN_RUN_ID"], "run-123")
        self.assertEqual(env["MN_RUNS_ROOT"], str(runs_root))
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(injected_config["identity"]["run_id"], "run-123")
        self.assertEqual(injected_config["outputs"]["run_root"], str(runs_root))
        self.assertEqual(job_mapping["job_id"], "job-blueprint-1")
        self.assertEqual(job_mapping["blueprint_id"], "worker_one")
        self.assertFalse((repo / "blueprints" / "worker_one" / "runs").exists())
        self.assertEqual(payloads, {"payload.txt": b"hello"})

    @patch('mn_api.state.client')
    def test_blueprint_run_generates_run_id_when_missing(self, mock_client):
        mock_client.submit_job.return_value = "job-blueprint-generated"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    response = self.client.post("/api/v1/blueprints/worker_one/runs", json={})
                    body = response.json()
                    mapping_exists = (runs_root / body["run_id"] / "job.json").exists()
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["job_id"], "job-blueprint-generated")
        self.assertTrue(body["run_id"].startswith("worker_one-"))
        manifest_json, _payloads = mock_client.submit_job.call_args.args
        manifest = json.loads(manifest_json)
        self.assertEqual(manifest["metadata"]["run_id"], body["run_id"])
        self.assertEqual(manifest["metadata"]["blueprint_run_id"], body["run_id"])
        self.assertTrue(mapping_exists)

    @patch('mn_api.state.client')
    def test_blueprint_run_starts_pre_launch_before_submit(self, mock_client):
        mock_client.submit_job.return_value = "job-pre-launch"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            script_path = repo / "worker_one" / "scripts" / "pre-launch.sh"
            script_path.parent.mkdir()
            script_path.write_text("#!/usr/bin/env bash\n")
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "identity": {"blueprint_id": "worker_one"},
                "video_source": {"uri": "rtsp://127.0.0.1:8554/video-watch"},
            }))
            process = SimpleNamespace(pid=6262, poll=lambda: None)
            captured_env = {}

            def fake_popen(_command, **kwargs):
                captured_env.update(kwargs["env"])
                Path(kwargs["env"]["MN_PRE_LAUNCH_READY_FILE"]).write_text("ready\n")
                return process

            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    with patch('mn_api.blueprints.subprocess.Popen', side_effect=fake_popen) as popen:
                        response = self.client.post(
                            "/api/v1/blueprints/worker_one/runs",
                            json={"run_id": "run-pre-launch"},
                        )
                        process_info = json.loads((runs_root / "run-pre-launch" / "pre_launch_process.json").read_text())
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "job-pre-launch")
        self.assertEqual(popen.call_args.args[0], ["bash", str(script_path.resolve())])
        self.assertEqual(captured_env["MN_RUN_ID"], "run-pre-launch")
        self.assertEqual(captured_env["MN_BLUEPRINT_BUNDLE_DIR"], str((repo / "worker_one").resolve()))
        self.assertEqual(json.loads(captured_env["MN_BLUEPRINT_CONFIG_JSON"])["identity"]["run_id"], "run-pre-launch")
        self.assertEqual(process_info["pid"], 6262)

    def test_blueprint_validate_runs_input_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text(json.dumps({
                "graph_id": "worker_one_graph",
                "nodes": [],
                "edges": [],
                "metadata": {},
                "input_validation": {
                    "rules": [
                        {
                            "name": "model_url",
                            "type": "pattern",
                            "path": "llm.api_base",
                            "pattern": "^https?://",
                        }
                    ]
                },
            }))
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "llm": {"api_base": "not-a-url"}
            }))
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post("/api/v1/blueprints/worker_one/validate")
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["validation"]["ok"])
        self.assertIn("model_url", body["validation"]["errors"][0])
        self.assertEqual(body["validation"]["issues"][0]["location"]["path"], "llm.api_base")
        self.assertEqual(body["validation"]["issues"][0]["rule"]["name"], "model_url")

    @patch('mn_api.state.client')
    def test_blueprint_run_validation_failure_blocks_submit_unless_forced(self, mock_client):
        mock_client.submit_job.return_value = "job-forced"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text(json.dumps({
                "graph_id": "worker_one_graph",
                "nodes": [],
                "edges": [],
                "metadata": {},
                "input_validation": {
                    "rules": [
                        {
                            "name": "model_url",
                            "type": "pattern",
                            "path": "llm.api_base",
                            "pattern": "^https?://",
                        }
                    ]
                },
            }))
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "llm": {"api_base": "not-a-url"}
            }))
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(repo / "runs")}):
                    failed = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-validate"},
                    )
                    forced = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-force", "force": True},
                    )
            finally:
                self._restore_config(original)

        self.assertEqual(failed.status_code, 422)
        self.assertEqual(failed.headers["content-type"], "application/problem+json")
        self.assertEqual(failed.json()["error"], "input_validation_failed")
        self.assertEqual(failed.json()["errors"][0]["location"]["path"], "llm.api_base")
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.json()["job_id"], "job-forced")
        manifest_json, _payloads = mock_client.submit_job.call_args.args
        self.assertTrue(json.loads(manifest_json)["metadata"]["mn_validation"]["force"])
        self.assertEqual(json.loads(manifest_json)["metadata"]["mn_validation"]["status"], "skipped")
        self.assertTrue(mock_client.submit_job.call_args.kwargs["force"])

    @patch('mn_api.state.client')
    def test_blueprint_run_rejects_invalid_run_id_before_submit(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post(
                    "/api/v1/blueprints/worker_one/runs",
                    json={"run_id": "../bad"},
                )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid run id")
        mock_client.submit_job.assert_not_called()

    @patch('mn_api.state.client')
    def test_blueprint_run_rejects_invalid_config_override_format(self, mock_client):
        response = self.client.post(
            "/api/v1/blueprints/worker_one/runs",
            json={"config_overwrite": ["not", "an", "object"]},
        )

        self.assertEqual(response.status_code, 422)
        mock_client.submit_job.assert_not_called()

    @patch('mn_api.state.client')
    def test_blueprint_run_rejects_malformed_manifest_before_submit(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text("{not json")
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post(
                    "/api/v1/blueprints/worker_one/runs",
                    json={"run_id": "run-123"},
                )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "blueprint manifest.json is malformed")
        mock_client.submit_job.assert_not_called()

    def test_blueprint_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            (repo / "index.json").write_text(json.dumps([
                {
                    "id": "worker_one",
                    "name": "Worker One",
                    "path": "../outside",
                }
            ]))
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post("/api/v1/blueprints/worker_one/install")
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "blueprint path escapes repository")

    def test_invalid_blueprint_id_and_missing_blueprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                invalid_response = self.client.get("/api/v1/blueprints/bad$id")
                missing_response = self.client.get("/api/v1/blueprints/missing_worker")
            finally:
                self._restore_config(original)

        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(missing_response.status_code, 404)

    def test_missing_and_malformed_blueprint_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_repo = Path(tmpdir) / "missing"
            original = self._set_blueprint_config(missing_repo)
            try:
                missing_response = self.client.get("/api/v1/blueprints")
            finally:
                self._restore_config(original)

            malformed_repo = Path(tmpdir) / "malformed"
            malformed_repo.mkdir()
            (malformed_repo / "index.json").write_text("{not json")
            original = self._set_blueprint_config(malformed_repo)
            try:
                malformed_response = self.client.get("/api/v1/blueprints")
            finally:
                self._restore_config(original)

        self.assertEqual(missing_response.status_code, 500)
        self.assertEqual(malformed_response.status_code, 500)

    def test_auth_applies_to_blueprint_endpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo, token="secret")
            try:
                unauthenticated = self.client.get("/api/v1/blueprints")
                authenticated = self.client.get(
                    "/api/v1/blueprints",
                    headers={"Authorization": "Bearer secret"},
                )
                install_unauthenticated = self.client.post("/api/v1/blueprints/worker_one/install")
                run_unauthenticated = self.client.post("/api/v1/blueprints/worker_one/runs", json={})
            finally:
                self._restore_config(original)

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(install_unauthenticated.status_code, 401)
        self.assertEqual(run_unauthenticated.status_code, 401)

if __name__ == '__main__':
    unittest.main()
