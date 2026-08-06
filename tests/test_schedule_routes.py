import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mn_api.main import app


class TestScheduleRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("mn_api.state.client")
    def test_list_schedules_proxies_filters(self, mock_client):
        mock_client.list_schedules.return_value = json.dumps({"schedules": []})

        response = self.client.get("/api/v2/schedules", params={"kind": "cron", "status": "paused"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"schedules": []})
        mock_client.list_schedules.assert_called_once_with(kind="cron", status="paused")

    @patch("mn_api.state.client")
    def test_create_schedule_proxies_manifest_payloads_and_default_source(self, mock_client):
        mock_client.create_schedule.return_value = json.dumps({"id": "sched-1"})

        response = self.client.post(
            "/api/v2/schedules",
            json={
                "manifest_json": '{"graph_id": "graph"}',
                "payloads": {"input.txt": "hello"},
                "schedule": {"kind": "cron"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "sched-1"})
        mock_client.create_schedule.assert_called_once_with(
            '{"graph_id": "graph"}',
            {"input.txt": b"hello"},
            schedule={"kind": "cron"},
            source={"api": "create_schedule"},
        )

    @patch("mn_api.state.client")
    def test_create_schedule_requires_manifest_or_bundle_before_client_call(self, mock_client):
        response = self.client.post("/api/v2/schedules", json={})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "manifest_json or _bundle_path is required")
        mock_client.create_schedule.assert_not_called()
