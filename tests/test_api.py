import unittest
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
