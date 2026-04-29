import unittest
import io
import json
import tempfile
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from types import SimpleNamespace
from mn_api import main
from mn_api.main import app
from unittest.mock import patch
import grpc

class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "auth": "disabled"})

    def test_auth_required_when_token_configured(self):
        original = main.config
        main.config = SimpleNamespace(api_token="secret", request_size_limit_bytes=1024 * 1024)
        try:
            response = self.client.get("/api/v1/system/summary")
            self.assertEqual(response.status_code, 401)
            response = self.client.get(
                "/api/v1/system/summary",
                headers={"Authorization": "Bearer secret"},
            )
            self.assertIn(response.status_code, (200, 500))
        finally:
            main.config = original

    def test_request_size_limit(self):
        original = main.config
        main.config = SimpleNamespace(api_token="", request_size_limit_bytes=10)
        try:
            response = self.client.post(
                "/api/v1/jobs",
                headers={"content-length": "11"},
                json={"manifest_json": "{}", "payloads": {}},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"], "request_too_large")
        finally:
            main.config = original

    @patch('mn_api.main.client')
    def test_list_jobs_success(self, mock_client):
        mock_client.list_jobs.return_value = '{"data": [{"job_id": "job-1"}]}'
        response = self.client.get("/api/v1/jobs?limit=5&include_terminal=false")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": [{"job_id": "job-1"}]})
        mock_client.list_jobs.assert_called_once_with(5, False)

    @patch('mn_api.main.client')
    def test_cleanup_jobs_success(self, mock_client):
        mock_client.clear_jobs.return_value = 3
        response = self.client.post("/api/v1/jobs/cleanup")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"cleared_count": 3})
        mock_client.clear_jobs.assert_called_once()

    @patch('mn_api.main.client')
    def test_get_system_summary_success(self, mock_client):
        mock_client.get_system_summary.return_value = '{"nodes": [], "jobs": []}'
        response = self.client.get("/api/v1/system/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"nodes": [], "jobs": []})

    @patch('mn_api.main.client')
    def test_submit_job_success(self, mock_client):
        mock_client.submit_job.return_value = "job-123"
        response = self.client.post(
            "/api/v1/jobs",
            json={"manifest_json": '{"graph_id": "g"}', "payloads": {"a.txt": "hello"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "job-123", "status": "pending"})
        mock_client.submit_job.assert_called_once_with('{"graph_id": "g"}', {"a.txt": b"hello"})

    @patch('mn_api.main.client')
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

    @patch('mn_api.main.client')
    def test_cancel_job_success(self, mock_client):
        mock_client.cancel_job.return_value = "cancelled"
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "cancelled", "job_id": "test_job_123"})

    @patch('mn_api.main.client')
    def test_cancel_job_grpc_error(self, mock_client):
        class MockRpcError(Exception):
            def details(self):
                return "job test_job_123 was not found"
                
        mock_client.cancel_job.side_effect = MockRpcError()
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "job test_job_123 was not found"})

    @patch('mn_api.main.client')
    def test_cancel_job_generic_error(self, mock_client):
        mock_client.cancel_job.side_effect = Exception("Some generic error")
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "Some generic error"})

    @patch('mn_api.main.client')
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

    @patch('mn_api.main.client')
    def test_get_job_events_success(self, mock_client):
        mock_client.stream_events.return_value = ['{"id": "e1"}', '{"id": "e2"}']
        response = self.client.get("/api/v1/jobs/test_job_123/events")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": [{"id": "e1"}, {"id": "e2"}]})

    @patch('mn_api.main.client')
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

    @patch('mn_api.main.client')
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

    @patch('mn_api.main.client')
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

    @patch('mn_api.main.client')
    def test_get_job_dead_letters_success(self, mock_client):
        mock_client.stream_events.return_value = [
            '{"type": "agent_started"}',
            '{"type": "dead_letter", "agent_id": "slow", "reason": "queue full"}',
        ]
        response = self.client.get("/api/v1/jobs/test_job_123/dead-letters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["reason"], "queue full")

    @patch('mn_api.main.client')
    def test_metrics_success(self, mock_client):
        mock_client.get_system_summary.return_value = '{"nodes": ["n1"], "jobs": [{"status": "running"}, {"status": "failed"}]}'
        response = self.client.get("/api/v1/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["jobs"]["by_status"], {"running": 1, "failed": 1})

    @patch('mn_api.main.client')
    def test_pause_and_resume_job_success(self, mock_client):
        mock_client.pause_job.return_value = "paused"
        pause_response = self.client.post("/api/v1/jobs/test_job_123/pause")
        self.assertEqual(pause_response.status_code, 200)
        self.assertEqual(pause_response.json(), {"status": "paused", "job_id": "test_job_123"})

        mock_client.resume_job.return_value = "running"
        resume_response = self.client.post("/api/v1/jobs/test_job_123/resume")
        self.assertEqual(resume_response.status_code, 200)
        self.assertEqual(resume_response.json(), {"status": "running", "job_id": "test_job_123"})

if __name__ == '__main__':
    unittest.main()
