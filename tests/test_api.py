import unittest
import importlib.util
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from types import SimpleNamespace
from mn_api.config import ApiConfig
from mn_api import state
from mn_api.main import app
from mn_api.blueprints import parse_cli_field, scheduler_allocated_ports_from_jobs_payload
from mn_api.routes.blueprints import runtime_blueprint_web_ui_reserved_ports, service_ports_from_payload
from unittest.mock import Mock, patch
import grpc


class FakeStreamingResponse:
    status = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return iter(self.lines)


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
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["auth"], "disabled")
        self.assertIn("blueprint_repo", body)
        self.assertIn("runs_root", body)

    @patch('mn_api.routes.models.assess_model_compatibility')
    @patch('mn_api.routes.models.load_model_ownership')
    @patch('mn_api.routes.models.state.client')
    @patch('mn_api.routes.models.subprocess.run')
    def test_models_route_lists_installed_docker_models(self, mock_run, mock_client, mock_ownership, mock_compatibility):
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"Name":"ai/gemma4:E2B"}\n',
            stderr="",
        )
        mock_client.get_system_summary.return_value = json.dumps({
            "nodes": [{"name": "mirror_neuron@local", "self": True}]
        })
        mock_ownership.return_value = {"version": 1, "models": {}}
        mock_compatibility.return_value = SimpleNamespace(to_dict=lambda: {
            "status": "pass",
            "ok": True,
            "message": "ready",
            "warnings": [],
        })

        response = self.client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["runner_available"])
        self.assertEqual(body["node"], "mirror_neuron@local")
        self.assertEqual(body["models"][0]["id"], "gemma4:e2b")
        self.assertEqual(body["models"][0]["docker_model"], "ai/gemma4:E2B")
        self.assertEqual(body["models"][0]["node"], "mirror_neuron@local")
        self.assertEqual(body["models"][0]["compatibility"]["status"], "pass")

    @patch('mn_api.routes.models.assess_model_compatibility')
    @patch('mn_api.routes.models.load_model_ownership')
    @patch('mn_api.routes.models.state.client')
    @patch('mn_api.routes.models.subprocess.run')
    def test_models_route_includes_persisted_ownership_metadata(self, mock_run, mock_client, mock_ownership, mock_compatibility):
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"Name":"ai/gemma4:E2B"}\n',
            stderr="",
        )
        mock_client.get_system_summary.return_value = json.dumps({
            "nodes": [{"name": "mirror_neuron@gpu-node", "self": True}]
        })
        mock_ownership.return_value = {
            "version": 1,
            "models": {
                "ai/gemma4:E2B": {
                    "model_id": "gemma4:e2b",
                    "docker_model": "ai/gemma4:E2B",
                    "provider": "docker_model_runner",
                    "manual": False,
                    "owners": {
                        "invoice": {"blueprint_id": "invoice"},
                        "research": {"blueprint_id": "research"},
                    },
                }
            },
        }
        mock_compatibility.return_value = SimpleNamespace(to_dict=lambda: {
            "status": "pass",
            "ok": True,
            "message": "ready",
            "warnings": [],
        })

        response = self.client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        model = response.json()["models"][0]
        self.assertEqual(model["node"], "mirror_neuron@gpu-node")
        self.assertEqual(model["nodes"], ["mirror_neuron@gpu-node"])
        self.assertEqual(model["owner_count"], 2)
        self.assertEqual(model["used_by"], ["invoice", "research"])
        self.assertFalse(model["orphaned"])

    @patch('mn_api.routes.models.assess_model_compatibility')
    @patch('mn_api.routes.models.load_model_ownership')
    @patch('mn_api.routes.models.state.client')
    @patch('mn_api.routes.models.subprocess.run')
    def test_models_route_reports_manual_installs_as_not_orphaned(self, mock_run, mock_client, mock_ownership, mock_compatibility):
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"Name":"ai/gemma4:E2B"}\n',
            stderr="",
        )
        mock_client.get_system_summary.return_value = json.dumps({
            "nodes": [{"name": "mirror_neuron@gpu-node", "self": True}]
        })
        mock_ownership.return_value = {
            "version": 1,
            "models": {
                "ai/gemma4:E2B": {
                    "model_id": "gemma4:e2b",
                    "docker_model": "ai/gemma4:E2B",
                    "provider": "docker_model_runner",
                    "manual": True,
                    "owners": {},
                }
            },
        }
        mock_compatibility.return_value = SimpleNamespace(to_dict=lambda: {
            "status": "pass",
            "ok": True,
            "message": "ready",
            "warnings": [],
        })

        response = self.client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        model = response.json()["models"][0]
        self.assertTrue(model["installed"])
        self.assertTrue(model["manual"])
        self.assertEqual(model["owner_count"], 0)
        self.assertEqual(model["used_by"], [])
        self.assertFalse(model["orphaned"])

    @patch('mn_api.routes.models.assess_model_compatibility')
    @patch('mn_api.routes.models.load_model_ownership')
    @patch('mn_api.routes.models.state.client')
    @patch('mn_api.routes.models.subprocess.run')
    def test_models_route_includes_external_installed_model_from_ownership(self, mock_run, mock_client, mock_ownership, mock_compatibility):
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"Name":"custom/local:latest"}\n',
            stderr="",
        )
        mock_client.get_system_summary.return_value = json.dumps({
            "nodes": [{"name": "mirror_neuron@local", "self": True}]
        })
        mock_ownership.return_value = {
            "version": 1,
            "models": {
                "custom/local:latest": {
                    "model_id": "custom-local",
                    "docker_model": "custom/local:latest",
                    "provider": "docker_model_runner",
                    "backend": "llama.cpp",
                    "manual": True,
                    "owners": {},
                }
            },
        }
        mock_compatibility.return_value = SimpleNamespace(to_dict=lambda: {
            "status": "unknown",
            "ok": False,
            "message": "external model",
            "warnings": [],
        })

        response = self.client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["runner_available"])
        model = body["models"][0]
        self.assertEqual(model["id"], "custom-local")
        self.assertEqual(model["docker_model"], "custom/local:latest")
        self.assertTrue(model["installed"])
        self.assertTrue(model["manual"])
        self.assertEqual(model["node"], "mirror_neuron@local")
        self.assertEqual(model["nodes"], ["mirror_neuron@local"])
        self.assertFalse(model["orphaned"])

    @patch('mn_api.routes.models.assess_model_compatibility')
    @patch('mn_api.routes.models.load_model_ownership')
    @patch('mn_api.routes.models.state.client')
    @patch('mn_api.routes.models.dmr_api_list_models')
    @patch('mn_api.routes.models.subprocess.run')
    def test_models_route_uses_dmr_api_when_docker_model_cli_is_missing(self, mock_run, mock_api_list, mock_client, mock_ownership, mock_compatibility):
        mock_run.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="unknown command: docker model",
        )
        mock_api_list.return_value = {"ai/gemma4:E2B"}
        mock_client.get_system_summary.return_value = json.dumps({
            "nodes": [{"name": "mirror_neuron@local", "self": True}]
        })
        mock_ownership.return_value = {"version": 1, "models": {}}
        mock_compatibility.return_value = SimpleNamespace(to_dict=lambda: {
            "status": "pass",
            "ok": True,
            "message": "ready",
            "warnings": [],
        })

        response = self.client.get("/api/v1/models")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["runner_available"])
        self.assertEqual(body["models"][0]["docker_model"], "ai/gemma4:E2B")
        mock_api_list.assert_called_once()

    @patch('mn_api.routes.models.state.client')
    @patch('mn_api.routes.models.subprocess.run')
    @patch('mn_api.routes.models.urllib.request.urlopen')
    def test_model_benchmark_streams_against_docker_model_runner(self, mock_urlopen, mock_run, mock_client):
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"Name":"ai/gemma4:E2B"}\n',
            stderr="",
        )
        mock_client.get_system_summary.return_value = json.dumps({
            "nodes": [{"name": "mirror_neuron@local", "self": True}]
        })
        mock_urlopen.return_value = FakeStreamingResponse([
            b'data: {"choices":[{"delta":{"content":"Ready"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" now"}}]}\n\n',
            b'data: [DONE]\n\n',
        ])

        response = self.client.post(
            "/api/v1/models/gemma4%3Ae2b/benchmark",
            json={"max_tokens": 16},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["docker_model"], "ai/gemma4:E2B")
        self.assertEqual(body["node"], "mirror_neuron@local")
        self.assertEqual(body["sample"], "Ready now")
        self.assertGreater(body["tokens_per_second"], 0)
        self.assertIsNotNone(body["first_token_ms"])
        request = mock_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/engines/v1/chat/completions"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "ai/gemma4:E2B")
        self.assertTrue(payload["stream"])

    @patch('mn_api.state.client')
    def test_service_routes_proxy_runtime_registry(self, mock_client):
        mock_client.list_services.return_value = json.dumps({"services": [{"name": "blueprint-web-ui"}]})
        mock_client.resolve_service.return_value = json.dumps({"services": [{"name": "blueprint-web-ui"}]})

        list_response = self.client.get("/api/v1/services", params={"tag": "web_ui", "job_id": "job-1"})
        resolve_response = self.client.get(
            "/api/v1/services/blueprint-web-ui/resolve",
            params={"tag": "video_watch_assistant", "passing_only": "false"},
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(resolve_response.status_code, 200)
        mock_client.list_services.assert_called_once_with(
            name=None,
            node=None,
            job_id="job-1",
            agent_id=None,
            status=None,
            tags=["web_ui"],
            passing_only=True,
        )
        mock_client.resolve_service.assert_called_once_with(
            "blueprint-web-ui",
            node=None,
            job_id=None,
            agent_id=None,
            tags=["video_watch_assistant"],
            passing_only=False,
        )

    @patch('mn_api.state.client')
    def test_run_ui_falls_back_to_registered_web_ui_service(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_root = Path(tmpdir)
            run_dir = runs_root / "run-with-service-ui"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({"job_id": "job-service-ui"}))
            mock_client.resolve_service.return_value = json.dumps({
                "services": [
                    {
                        "id": "job-service-ui:web_ui_dashboard:blueprint-web-ui",
                        "name": "blueprint-web-ui",
                        "job_id": "job-service-ui",
                        "status": "warning",
                        "address": "127.0.0.1",
                        "port": 58101,
                        "meta": {
                            "run_id": "run-with-service-ui",
                            "blueprint_id": "video_watch_assistant",
                            "title": "Video Dashboard",
                            "adapter": "gradio",
                            "url": "http://localhost:58101",
                            "browser_video_source": "http://127.0.0.1:8889/video-watch/",
                            "run_ui_path": str(run_dir / "ui.json"),
                        },
                    }
                ]
            })

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/runs/run-with-service-ui/ui")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["web_ui"]["url"], "http://localhost:58101")
        self.assertEqual(body["web_ui"]["status"], "warning")
        self.assertEqual(body["ui"]["metadata"]["service_name"], "blueprint-web-ui")
        mock_client.resolve_service.assert_called_once_with(
            "blueprint-web-ui",
            job_id="job-service-ui",
            tags=["web_ui"],
            passing_only=False,
        )

    @patch('mn_api.state.client')
    def test_run_ui_falls_back_to_persisted_web_ui_service_contract(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_root = Path(tmpdir)
            run_dir = runs_root / "run-with-persisted-ui"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-persisted-ui",
                "run_id": "run-with-persisted-ui",
                "web_ui_service": {
                    "node_id": "web_ui_dashboard",
                    "service_name": "blueprint-web-ui",
                    "run_id": "run-with-persisted-ui",
                    "blueprint_id": "video_watch_assistant",
                    "title": "Video Dashboard",
                    "adapter": "gradio",
                    "url": "http://localhost:61000",
                    "host": "127.0.0.1",
                    "address": "127.0.0.1",
                    "port": 61000,
                    "browser_video_source": "http://127.0.0.1:8889/video-watch/",
                },
            }))
            mock_client.resolve_service.side_effect = RuntimeError("gRPC auth token is required for this RPC")

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                with patch("mn_api.routes.runs._web_ui_url_status", return_value="running"):
                    response = self.client.get("/api/v1/runs/run-with-persisted-ui/ui")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["web_ui"]["url"], "http://localhost:61000")
        self.assertEqual(body["web_ui"]["status"], "running")
        self.assertEqual(body["web_ui"]["metadata"]["registered_by"], "blueprint_job_mapping")
        self.assertEqual(body["ui"]["metadata"]["registered_by"], "blueprint_job_mapping")
        self.assertEqual(body["ui"]["components"][0]["browser_source"], "http://127.0.0.1:8889/video-watch/")

    @patch('mn_api.state.client')
    def test_run_ui_falls_back_to_event_relay_web_ui_service_contract(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            runs_root = Path(tmpdir)
            run_dir = runs_root / "run-with-relay-ui"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-relay-ui",
                "run_id": "run-with-relay-ui",
            }))
            (run_dir / "event_relay.json").write_text(json.dumps({
                "job_id": "job-relay-ui",
                "run_id": "run-with-relay-ui",
                "service": {
                    "node_id": "web_ui_dashboard",
                    "service_name": "blueprint-web-ui",
                    "run_id": "run-with-relay-ui",
                    "blueprint_id": "video_watch_assistant",
                    "title": "Video Dashboard",
                    "adapter": "gradio",
                    "url": "http://localhost:61000",
                    "host": "0.0.0.0",
                    "address": "127.0.0.1",
                    "port": 61000,
                    "browser_video_source": "http://127.0.0.1:8889/video-watch/",
                },
            }))
            mock_client.resolve_service.side_effect = RuntimeError("gRPC auth token is required for this RPC")

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                with patch("mn_api.routes.runs._web_ui_url_status", return_value="running"):
                    response = self.client.get("/api/v1/runs/run-with-relay-ui/ui")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["web_ui"]["url"], "http://localhost:61000")
        self.assertEqual(body["web_ui"]["metadata"]["registered_by"], "blueprint_job_mapping")
        self.assertEqual(body["ui"]["components"][0]["browser_source"], "http://127.0.0.1:8889/video-watch/")

    def test_config_uses_grpc_auth_token(self):
        with patch.dict(os.environ, {"MN_GRPC_AUTH_TOKEN": "auth-secret"}, clear=False):
            config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-secret")

    def test_service_ports_from_payload_extracts_valid_ports(self):
        self.assertEqual(
            service_ports_from_payload(
                {
                    "services": [
                        {"name": "blueprint-web-ui", "port": 61000},
                        {"name": "blueprint-web-ui", "port": "61001"},
                        {"name": "blueprint-web-ui", "port": "not-a-port"},
                        {"name": "other"},
                    ]
                }
            ),
            {61000, 61001},
        )

    def test_service_ports_from_payload_can_filter_to_active_jobs(self):
        self.assertEqual(
            service_ports_from_payload(
                {
                    "services": [
                        {"job_id": "active-job", "port": 61000},
                        {"job_id": "terminal-job", "port": 61001},
                        {"port": 61002},
                    ]
                },
                active_job_ids={"active-job"},
            ),
            {61000},
        )

    def test_service_ports_from_payload_can_filter_to_live_services(self):
        self.assertEqual(
            service_ports_from_payload(
                {
                    "services": [
                        {"job_id": "missing-job", "status": "passing", "port": 61000},
                        {"job_id": "critical-job", "status": "critical", "port": 61001},
                        {"job_id": "failed-health-job", "status": "warning", "health": {"status": "critical"}, "port": 61002},
                        {"job_id": "unknown-status-job", "port": 61003},
                    ]
                },
                live_only=True,
            ),
            {61000, 61003},
        )

    def test_scheduler_allocated_ports_from_jobs_payload_extracts_web_ui_ports(self):
        self.assertEqual(
            scheduler_allocated_ports_from_jobs_payload(
                {
                    "job": {
                        "job_id": "active-job",
                        "scheduler": {
                            "placements": [
                                {
                                    "agent_id": "web_ui_dashboard",
                                    "allocations": {
                                        "ports": [
                                            {"label": "web_ui", "port": 61000, "protocol": "http"},
                                            {"label": "debug", "port": "61001"},
                                            {"label": "bad", "port": "not-a-port"},
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                }
            ),
            {61000, 61001},
        )

    @patch("mn_api.state.client")
    def test_runtime_blueprint_web_ui_reserved_ports_falls_back_to_job_placements(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps(
            {"data": [{"job_id": "active-job", "status": "running"}]}
        )
        mock_client.get_job.return_value = json.dumps(
            {
                "job": {
                    "job_id": "active-job",
                    "status": "running",
                    "scheduler": {
                        "placements": [
                            {
                                "agent_id": "web_ui_dashboard",
                                "allocations": {
                                    "ports": [{"label": "web_ui", "port": 61000, "protocol": "http"}]
                                },
                            }
                        ]
                    },
                }
            }
        )
        mock_client.resolve_service.side_effect = RuntimeError("gRPC auth token is required for this RPC")

        self.assertEqual(runtime_blueprint_web_ui_reserved_ports(), {61000})
        mock_client.get_job.assert_called_once_with("active-job")

    @patch("mn_api.state.client")
    def test_runtime_blueprint_web_ui_reserved_ports_uses_live_registry_when_jobs_are_missing(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps({"data": []})
        mock_client.resolve_service.return_value = json.dumps({
            "services": [
                {"job_id": "registry-live-job", "status": "passing", "port": 61000},
                {"job_id": "registry-stale-job", "status": "critical", "port": 61001},
            ]
        })

        self.assertEqual(runtime_blueprint_web_ui_reserved_ports(), {61000})
        mock_client.get_job.assert_not_called()

    @unittest.skipIf(
        importlib.util.find_spec("mn_blueprint_support") is None,
        "mn_blueprint_support is not installed",
    )
    @patch('mn_api.state.client')
    def test_blueprint_run_persists_runtime_web_ui_service_contract(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps({"data": []})
        mock_client.resolve_service.return_value = json.dumps({"services": []})
        mock_client.submit_job.return_value = "job-web-ui-contract"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = repo / "runs"
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text(json.dumps({
                "graph_id": "worker_one_graph",
                "type": "service",
                "nodes": [],
                "edges": [],
                "metadata": {},
            }))
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "identity": {"blueprint_id": "worker_one", "name": "Worker One"},
                "web_ui": {
                    "enabled": True,
                    "dashboard": {
                        "browser_video_source": "http://127.0.0.1:8889/video-watch/",
                    },
                    "output": {
                        "adapter": "gradio",
                        "auto_generate": True,
                        "title": "Worker One Dashboard",
                    },
                },
            }))
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict(os.environ, {
                    "MN_RUNS_ROOT": str(runs_root),
                    "MN_BLUEPRINT_WEB_UI_PORT_START": "61000",
                    "MN_BLUEPRINT_WEB_UI_PORT_END": "61000",
                    "MN_BLUEPRINT_WEB_UI_PORT_ALLOCATION_MODE": "prepublished",
                }):
                    response = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-web-ui-contract", "force": True},
                    )
                    job_record = json.loads((runs_root / "run-web-ui-contract" / "job.json").read_text())
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["web_ui_service"]["service_name"], "blueprint-web-ui")
        self.assertEqual(body["web_ui_service"]["url"], "http://localhost:61000")
        self.assertEqual(job_record["web_ui_service"]["run_id"], "run-web-ui-contract")
        self.assertEqual(job_record["web_ui_service"]["port"], 61000)
        self.assertEqual(job_record["web_ui_service"]["browser_video_source"], "http://127.0.0.1:8889/video-watch/")

    def test_config_uses_grpc_admin_token(self):
        with patch.dict(os.environ, {"MN_GRPC_ADMIN_TOKEN": "admin-secret"}, clear=False):
            config = ApiConfig.from_env()

        self.assertEqual(config.grpc_admin_token, "admin-secret")

    def test_config_uses_legacy_grpc_admin_token(self):
        with patch.dict(
            os.environ,
            {
                "MN_GRPC_ADMIN_TOKEN": "",
                "MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN": "legacy-admin-secret",
            },
            clear=False,
        ):
            config = ApiConfig.from_env()

        self.assertEqual(config.grpc_admin_token, "legacy-admin-secret")

    def test_config_uses_dev_local_blueprint_repo_only_in_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "MN_ENV": "dev",
                    "MN_BLUEPRINT_REPO": "https://example.com/base-blueprints.git",
                    "MN_DEV_LOCAL_BLUEPRINT_REPO": tmp,
                },
                clear=False,
            ):
                config = ApiConfig.from_env()

        self.assertEqual(config.blueprint_repo, tmp)
        self.assertEqual(config.configured_blueprint_repo, "https://example.com/base-blueprints.git")
        self.assertEqual(config.dev_local_blueprint_repo, tmp)

    def test_config_ignores_blank_persisted_blueprint_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".mn"
            state_dir.mkdir()
            (state_dir / "docker-compose.env").write_text("MN_BLUEPRINT_REPO=\nMN_DEV_LOCAL_BLUEPRINT_REPO=\n")

            with patch.dict(os.environ, {"HOME": tmp}, clear=True):
                config = ApiConfig.from_env()

        self.assertEqual(config.blueprint_repo, "https://github.com/MirrorNeuronLab/mn-blueprints.git")

    def test_config_rejects_dev_local_blueprint_repo_in_prod(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "MN_ENV": "prod",
                    "MN_API_TOKEN": "api-secret",
                    "MN_DEV_LOCAL_BLUEPRINT_REPO": tmp,
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "MN_DEV_LOCAL_BLUEPRINT_REPO"):
                    ApiConfig.from_env()

    def test_config_uses_local_grpc_token_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp) / ".mn"
            token_dir.mkdir()
            (token_dir / "grpc_auth.token").write_text("auth-from-file\n")
            (token_dir / "grpc_admin.token").write_text("admin-from-file\n")

            with patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "MN_HOME": "",
                    "MIRROR_NEURON_HOME": "",
                    "MN_GRPC_AUTH_TOKEN": "",
                    "MN_GRPC_ADMIN_TOKEN": "",
                    "MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN": "",
                },
                clear=False,
            ):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-from-file")
        self.assertEqual(config.grpc_admin_token, "admin-from-file")

    def test_config_uses_local_grpc_token_files_before_stale_runtime_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp) / ".mn"
            token_dir.mkdir()
            (token_dir / "docker-compose.env").write_text(
                "MN_GRPC_AUTH_TOKEN=stale-auth-from-state\n"
                "MN_GRPC_ADMIN_TOKEN=stale-admin-from-state\n"
            )
            (token_dir / "grpc_auth.token").write_text("auth-from-file\n")
            (token_dir / "grpc_admin.token").write_text("admin-from-file\n")

            with patch.dict(os.environ, {"HOME": tmp}, clear=True):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-from-file")
        self.assertEqual(config.grpc_admin_token, "admin-from-file")

    def test_config_uses_configured_grpc_token_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp)
            auth_file = token_dir / "auth.token"
            admin_file = token_dir / "admin.token"
            auth_file.write_text("auth-from-configured-file\n")
            admin_file.write_text("admin-from-configured-file\n")

            with patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "MN_GRPC_AUTH_TOKEN_FILE": str(auth_file),
                    "MN_GRPC_ADMIN_TOKEN_FILE": str(admin_file),
                    "MN_GRPC_AUTH_TOKEN": "",
                    "MN_GRPC_ADMIN_TOKEN": "",
                    "MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN": "",
                },
                clear=True,
            ):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-from-configured-file")
        self.assertEqual(config.grpc_admin_token, "admin-from-configured-file")

    def test_config_uses_persisted_runtime_grpc_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".mn"
            state_dir.mkdir()
            (state_dir / "docker-compose.env").write_text(
                "\n".join(
                    [
                        "MN_API_HOST=127.0.0.1",
                        "MN_API_PORT=54111",
                        "MN_GRPC_PORT=55111",
                        "MN_CORE_GRPC_TARGET=127.0.0.1:55111",
                        "MN_GRPC_AUTH_TOKEN=auth-from-state",
                        "MN_GRPC_ADMIN_TOKEN=admin-from-state",
                    ]
                )
                + "\n"
            )

            with patch.dict(os.environ, {"HOME": tmp}, clear=True):
                config = ApiConfig.from_env()

        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 54111)
        self.assertEqual(config.grpc_target, "127.0.0.1:55111")
        self.assertEqual(config.grpc_auth_token, "auth-from-state")
        self.assertEqual(config.grpc_admin_token, "admin-from-state")

    def test_state_recreates_client_when_grpc_admin_token_changes(self):
        created = []

        class FakeChannel:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeClient:
            def __init__(self, target=None, timeout=None, auth_token=None, admin_token=None):
                self.kwargs = {
                    "target": target,
                    "timeout": timeout,
                    "auth_token": auth_token,
                    "admin_token": admin_token,
                }
                self.channel = FakeChannel()
                created.append(self)

        original_config = state.config
        original_client = state._client
        original_refresh = state.refresh_config_from_env
        try:
            state._client = None
            first = SimpleNamespace(
                grpc_target="127.0.0.1:55051",
                grpc_timeout_seconds=10.0,
                grpc_auth_token="auth-token",
                grpc_admin_token="admin-one",
            )
            second = SimpleNamespace(
                grpc_target="127.0.0.1:55051",
                grpc_timeout_seconds=10.0,
                grpc_auth_token="auth-token",
                grpc_admin_token="admin-two",
            )
            with patch("mn_api.state.Client", FakeClient):
                state.config = first
                refreshed_config = {"value": first}

                def fake_refresh_config_from_env():
                    refreshed = refreshed_config["value"]
                    if (
                        state._client is not None
                        and state._grpc_client_settings(refreshed)
                        != state._grpc_client_settings(state.config)
                    ):
                        state.close_client()
                    state.config = refreshed
                    return state.config

                state.refresh_config_from_env = fake_refresh_config_from_env
                self.assertEqual(state.get_client().kwargs["admin_token"], "admin-one")
                refreshed_config["value"] = second
                self.assertEqual(state.get_client().kwargs["admin_token"], "admin-two")

            self.assertEqual(len(created), 2)
            self.assertTrue(created[0].channel.closed)
        finally:
            state._client = original_client
            state.config = original_config
            state.refresh_config_from_env = original_refresh

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
    def test_list_jobs_reconciles_stale_paused_status_from_workflow_progress(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps({
            "data": [
                {
                    "job_id": "job-progress",
                    "status": "paused",
                    "job_type": "batch",
                    "recovery_status": "paused_for_review",
                }
            ]
        })

        with patch(
            "mn_api.routes.jobs._workflow_progress_snapshot_for_job",
            return_value={"job_id": "job-progress", "status": "running"},
        ) as mock_progress:
            response = self.client.get("/api/v1/jobs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["status"], "running")
        mock_progress.assert_called_once_with("job-progress")

    @patch('mn_api.state.client')
    def test_list_jobs_status_reconciliation_preserves_recovery_metadata(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps({
            "data": [
                {
                    "job_id": "job-review",
                    "status": "paused",
                    "recovery_status": "paused_for_review",
                    "recovery_requires_review": True,
                    "recovery": {
                        "status": "paused_for_review",
                        "reason": "worker restart attempts exhausted",
                        "can_resume": True,
                    },
                }
            ]
        })

        with patch(
            "mn_api.routes.jobs._workflow_progress_snapshot_for_job",
            return_value={"job_id": "job-review", "status": "running"},
        ):
            response = self.client.get("/api/v1/jobs")

        self.assertEqual(response.status_code, 200)
        row = response.json()["data"][0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["recovery_status"], "paused_for_review")
        self.assertTrue(row["recovery_requires_review"])
        self.assertEqual(row["recovery"]["reason"], "worker restart attempts exhausted")
        self.assertTrue(row["recovery"]["can_resume"])

    @patch('mn_api.state.client')
    def test_list_jobs_refreshes_active_rows_from_workflow_progress(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps({
            "data": [{"job_id": "job-progress", "status": "running"}]
        })

        with patch(
            "mn_api.routes.jobs._workflow_progress_snapshot_for_job",
            return_value={"job_id": "job-progress", "status": "completed"},
        ) as mock_progress:
            response = self.client.get("/api/v1/jobs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["status"], "completed")
        mock_progress.assert_called_once_with("job-progress")

    @patch('mn_api.state.client')
    def test_cleanup_jobs_success(self, mock_client):
        mock_client.clear_jobs.return_value = 3
        response = self.client.post("/api/v1/jobs:cleanup")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"cleared_count": 3})
        mock_client.clear_jobs.assert_called_once()

    def test_cleanup_jobs_retries_after_admin_token_mismatch(self):
        class PermissionDeniedRpcError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.PERMISSION_DENIED

            def details(self):
                return "ClearJobs requires MN_GRPC_ADMIN_TOKEN"

        first_client = SimpleNamespace(clear_jobs=Mock(side_effect=PermissionDeniedRpcError()))
        second_client = SimpleNamespace(clear_jobs=Mock(return_value=2))
        close_client = Mock()
        clients = [first_client, second_client]

        class ClientProxy:
            def __getattr__(self, name):
                return getattr(clients.pop(0), name)

        with patch("mn_api.routes.jobs.state.client", ClientProxy()), patch(
            "mn_api.routes.jobs.state.close_client",
            close_client,
        ):
            response = self.client.post("/api/v1/jobs:cleanup")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"cleared_count": 2})
        first_client.clear_jobs.assert_called_once()
        second_client.clear_jobs.assert_called_once()
        close_client.assert_called_once()

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
        self.assertEqual(response.json()["totals"]["memory_total_gb"], 0.0)
        self.assertEqual(response.json()["totals"]["gpu_memory_total_gb"], 0.0)
        self.assertEqual(response.json()["combined"]["cpu_cores"], 8)
        mock_client.get_resource.assert_called_once()

    @patch('mn_api.state.client')
    def test_get_resource_combines_multi_node_resources(self, mock_client):
        mock_client.get_resource.return_value = json.dumps({
            "mode": "cluster",
            "node_count": 2,
            "nodes": [
                {
                    "name": "mn1",
                    "cpu_cores": 8,
                    "cpu_model": "AMD Ryzen AI Max+ 395",
                    "gpu_count": 2,
                    "gpu_model": "NVIDIA RTX 4090",
                    "gpu_models": ["NVIDIA RTX 4090", "NVIDIA RTX 6000 Ada"],
                    "gpu_memory_total_mb": 48_000,
                    "gpu_memory_free_mb": 32_000,
                    "memory_gb": 16.0,
                },
                {"name": "mn2", "cpu_cores": 4, "gpu_count": 0, "memory_gb": 8.0},
            ],
        })

        response = self.client.get("/api/v1/resource")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["combined"]["cpu_cores"], 12)
        self.assertEqual(response.json()["combined"]["gpu_count"], 2)
        self.assertEqual(response.json()["combined"]["memory_gb"], 24.0)
        self.assertEqual(response.json()["combined"]["memory_total_gb"], 24.0)
        self.assertEqual(response.json()["combined"]["memory_available_gb"], 0.0)
        self.assertEqual(response.json()["combined"]["gpu_memory_total_gb"], 46.88)
        self.assertEqual(response.json()["nodes"][0]["name"], "mn1")
        self.assertEqual(response.json()["nodes"][0]["cpu_model"], "AMD Ryzen AI Max+ 395")
        self.assertEqual(response.json()["nodes"][0]["gpu_model"], "NVIDIA RTX 4090")
        self.assertEqual(
            response.json()["nodes"][0]["gpu_models"],
            ["NVIDIA RTX 4090", "NVIDIA RTX 6000 Ada"],
        )

    @patch('mn_api.state.client')
    def test_set_resource_success(self, mock_client):
        mock_client.set_resource.return_value = json.dumps({
            "limits": {"cpu": 50, "gpu": 75, "memory": 100},
            "totals": {
                "cpu_cores": 8,
                "gpu_count": 1,
                "gpu_memory_total_mb": 24_576,
                "gpu_memory_free_mb": 20_480,
                "memory_gb": 32.0,
            },
        })
        response = self.client.put(
            "/api/v1/resource",
            json={"cpu": 50, "gpu": 75, "memory": 100},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limits"]["gpu"], 75)
        self.assertEqual(response.json()["totals"]["gpu_memory_total_gb"], 24.0)
        self.assertEqual(response.json()["totals"]["memory_total_gb"], 32.0)
        mock_client.set_resource.assert_called_once_with({"cpu": 50, "gpu": 75, "memory": 100})

    @patch('mn_api.routes.system.Client')
    @patch('mn_api.state.client')
    def test_add_cluster_node_success(self, mock_client, mock_remote_client_class):
        remote_client = mock_remote_client_class.return_value
        remote_client.network_handshake.return_value = {
            "node_name": "mirror_neuron@10.0.0.42",
            "runtime_mode": "network_only",
            "grpc_host": "10.0.0.42",
            "grpc_port": 55051,
            "redis_url": "redis://:join-token@10.0.0.42:6379/0",
        }
        mock_client.add_node.return_value = "connected"

        response = self.client.post(
            "/api/v1/system/cluster/nodes:add",
            json={"host": "10.0.0.42", "token": "join-token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["host"], "10.0.0.42")
        self.assertEqual(body["node_name"], "mirror_neuron@10.0.0.42")
        self.assertEqual(body["status"], "connected")
        self.assertNotIn("redis_url", body["handshake"])
        self.assertNotIn("join-token", json.dumps(body))
        mock_remote_client_class.assert_called_once_with(target="10.0.0.42:55051", auth_token="", timeout=10)
        remote_client.network_handshake.assert_called_once()
        self.assertEqual(remote_client.network_handshake.call_args.args[0], "join-token")
        self.assertIn("node_name", remote_client.network_handshake.call_args.kwargs)
        self.assertIn("node_info", remote_client.network_handshake.call_args.kwargs)
        mock_client.add_node.assert_called_once_with("mirror_neuron@10.0.0.42", token="join-token")

    @patch('mn_api.routes.system.socket.gethostname', return_value="api-box")
    @patch('mn_api.routes.system.detect_lan_ip', return_value="192.168.1.9")
    @patch('mn_api.routes.system.Client')
    @patch('mn_api.state.client')
    def test_add_cluster_node_falls_back_when_default_grpc_port_unavailable(
        self,
        mock_client,
        mock_remote_client_class,
        _mock_detect_lan_ip,
        _mock_gethostname,
    ):
        class UnavailableRpcError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        first_remote = SimpleNamespace(
            network_handshake=Mock(side_effect=UnavailableRpcError())
        )
        second_remote = SimpleNamespace(
            network_handshake=Mock(
                return_value={
                    "node_name": "mirror_neuron@10.0.0.42",
                    "runtime_mode": "network_only",
                    "grpc_host": "10.0.0.42",
                    "grpc_port": 50051,
                    "redis_url": "redis://:join-token@10.0.0.42:6379/0",
                }
            )
        )
        mock_remote_client_class.side_effect = [first_remote, second_remote]
        mock_client.add_node.return_value = "connected"

        response = self.client.post(
            "/api/v1/system/cluster/nodes:add",
            json={"host": "10.0.0.42", "token": "join-token"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["node_name"], "mirror_neuron@10.0.0.42")
        self.assertEqual(body["status"], "connected")
        self.assertNotIn("join-token", json.dumps(body))
        self.assertEqual(
            [call.kwargs["target"] for call in mock_remote_client_class.call_args_list],
            ["10.0.0.42:55051", "10.0.0.42:50051"],
        )
        first_remote.network_handshake.assert_called_once()
        second_remote.network_handshake.assert_called_once_with(
            "join-token",
            node_name="mirror_neuron@192.168.1.9",
            node_info={
                "node_name": "mirror_neuron@192.168.1.9",
                "display_name": "api-box",
                "hostname": "api-box",
            },
        )
        mock_client.add_node.assert_called_once_with("mirror_neuron@10.0.0.42", token="join-token")

    @patch('mn_api.routes.system.socket.gethostname', return_value="api-box")
    @patch('mn_api.routes.system.detect_lan_ip', return_value="192.168.1.9")
    @patch('mn_api.routes.system.Client')
    @patch('mn_api.state.client')
    def test_add_cluster_node_uses_explicit_grpc_port_without_fallback(
        self,
        mock_client,
        mock_remote_client_class,
        _mock_detect_lan_ip,
        _mock_gethostname,
    ):
        remote_client = mock_remote_client_class.return_value
        remote_client.network_handshake.return_value = {
            "node_name": "mirror_neuron@10.0.0.42",
            "runtime_mode": "network_only",
            "grpc_host": "10.0.0.42",
            "grpc_port": 56051,
            "redis_url": "redis://:join-token@10.0.0.42:6379/0",
        }
        mock_client.add_node.return_value = "connected"

        response = self.client.post(
            "/api/v1/system/cluster/nodes:add",
            json={"host": "10.0.0.42", "token": "join-token", "grpc_port": 56051},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "connected")
        self.assertNotIn("join-token", json.dumps(body))
        mock_remote_client_class.assert_called_once_with(target="10.0.0.42:56051", auth_token="", timeout=10)
        remote_client.network_handshake.assert_called_once_with(
            "join-token",
            node_name="mirror_neuron@192.168.1.9",
            node_info={
                "node_name": "mirror_neuron@192.168.1.9",
                "display_name": "api-box",
                "hostname": "api-box",
            },
        )
        mock_client.add_node.assert_called_once_with("mirror_neuron@10.0.0.42", token="join-token")

    @patch('mn_api.state.client')
    def test_remove_cluster_node_success(self, mock_client):
        mock_client.remove_node.return_value = "disconnected"

        response = self.client.post(
            "/api/v1/system/cluster/nodes:remove",
            json={"node_name": "mirror_neuron@10.0.0.42"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "disconnected")
        mock_client.remove_node.assert_called_once_with("mirror_neuron@10.0.0.42")

    @patch('mn_api.routes.system.Client')
    @patch('mn_api.state.client')
    def test_add_cluster_node_rejects_invalid_host_before_handshake(self, mock_client, mock_remote_client_class):
        response = self.client.post(
            "/api/v1/system/cluster/nodes:add",
            json={"host": "http://10.0.0.42", "token": "join-token"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Remote node host must be a host name or IP address.")
        mock_remote_client_class.assert_not_called()
        mock_client.add_node.assert_not_called()

    @patch('mn_api.routes.system.Client')
    @patch('mn_api.state.client')
    def test_add_cluster_node_rejects_invalid_token_before_handshake(self, mock_client, mock_remote_client_class):
        response = self.client.post(
            "/api/v1/system/cluster/nodes:add",
            json={"host": "10.0.0.42", "token": "bad token"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Remote node token is required.")
        mock_remote_client_class.assert_not_called()
        mock_client.add_node.assert_not_called()

    @patch('mn_api.state.client')
    def test_submit_job_success(self, mock_client):
        mock_client.submit_job.return_value = "job-123"
        response = self.client.post(
            "/api/v1/jobs",
            json={"manifest_json": '{"graph_id": "g"}', "payloads": {"a.txt": "hello"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "job-123", "status": "pending"})
        manifest_json, payloads = mock_client.submit_job.call_args.args
        manifest = json.loads(manifest_json)
        self.assertEqual(manifest["graph_id"], "g")
        self.assertIn("mn_storage", manifest["metadata"])
        self.assertEqual(payloads, {"a.txt": b"hello"})

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
        manifest_json, payloads = mock_client.submit_job.call_args.args
        submitted_manifest = json.loads(manifest_json)
        self.assertEqual(submitted_manifest["graph_id"], "zip_graph")
        self.assertIn("mn_storage", submitted_manifest["metadata"])
        self.assertEqual(payloads, {"a.txt": b"hello"})

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

    @unittest.skipIf(
        importlib.util.find_spec("mn_blueprint_support") is None,
        "mn_blueprint_support is not installed",
    )
    def test_run_observability_endpoints_read_shared_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "observe-run"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(json.dumps({
                "run_id": "observe-run",
                "blueprint_id": "general_human_in_the_loop_workflow",
                "trace_id": "trc_observe",
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
            (run_dir / "timeline.jsonl").write_text(
                json.dumps({
                    "schema_version": "mn.timeline.v1",
                    "ts": "2026-05-22T12:00:04Z",
                    "run_id": "observe-run",
                    "blueprint_id": "general_human_in_the_loop_workflow",
                    "trace_id": "trc_observe",
                    "span_id": "spn_timeline",
                    "type": "run_started",
                    "status": "started",
                    "summary": "Run started",
                })
                + "\n"
            )
            (run_dir / "observability_summary.json").write_text(json.dumps({
                "schema_version": "mn.observability_summary.v1",
                "run_id": "observe-run",
                "trace_id": "trc_observe",
                "status": "running",
                "counts": {"events": 1, "logs": 1, "errors": 0, "timeline": 1},
            }))

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                logs = self.client.get("/api/v1/runs/observe-run/logs?level=INFO")
                timeline = self.client.get("/api/v1/runs/observe-run/timeline")
                observability_summary = self.client.get("/api/v1/runs/observe-run/observability-summary")
                human = self.client.get("/api/v1/runs/observe-run/human?status=pending")
                response = self.client.post(
                    "/api/v1/runs/observe-run/human/hitl-1/response",
                    json={"decision": "approve", "notes": "ok"},
                )
                resources = self.client.get("/api/v1/runs/observe-run/resources?window=24000h&bucket=1h")

        self.assertEqual(logs.status_code, 200)
        self.assertEqual(logs.json()["data"][0]["message"], "needs attention")
        self.assertEqual(timeline.status_code, 200)
        self.assertEqual(timeline.json()["data"][0]["trace_id"], "trc_observe")
        self.assertEqual(observability_summary.status_code, 200)
        self.assertEqual(observability_summary.json()["trace_id"], "trc_observe")
        self.assertEqual(human.status_code, 200)
        self.assertEqual(human.json()["data"][0]["payload"]["request_id"], "hitl-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"]["approved"], True)
        self.assertEqual(resources.status_code, 200)
        self.assertEqual(resources.json()["sample_count"], 1)

    def test_run_artifact_endpoints_read_shared_run_store_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "artifact-run"
            run_dir.mkdir()
            (run_dir / "result.json").write_text(json.dumps({"ok": True, "value": 42}))
            (run_dir / "final_artifact.json").write_text(json.dumps({"type": "prepared_1040_tax_packet"}))
            (run_dir / "errors.jsonl").write_text(json.dumps({"error": {"schema_version": "mn.error.v1"}}) + "\n")
            (run_dir / "errors.001.jsonl").write_text(json.dumps({"error": {"schema_version": "mn.error.v1"}}) + "\n")
            (run_dir / "timeline.jsonl").write_text(json.dumps({"schema_version": "mn.timeline.v1"}) + "\n")
            (run_dir / "timeline.json").write_text(json.dumps({"schema_version": "mn.timeline.compact.v1"}))
            (run_dir / "observability_summary.json").write_text(json.dumps({"schema_version": "mn.observability_summary.v1"}))
            (run_dir / "report.md").write_text("# Draft Review Packet\n")
            (run_dir / "packet.pdf").write_bytes(b"%PDF-1.4\n% test pdf\n")

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                result = self.client.get("/api/v1/runs/artifact-run/result")
                final_artifact = self.client.get("/api/v1/runs/artifact-run/final-artifact")
                listing = self.client.get("/api/v1/runs/artifact-run/artifacts")
                markdown = self.client.get("/api/v1/runs/artifact-run/artifacts/report.md")
                pdf = self.client.get("/api/v1/runs/artifact-run/artifacts/packet.pdf")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["value"], 42)
        self.assertEqual(final_artifact.status_code, 200)
        self.assertEqual(final_artifact.json()["type"], "prepared_1040_tax_packet")
        self.assertEqual(listing.status_code, 200)
        artifact_ids = {artifact["artifact_id"] for artifact in listing.json()["artifacts"]}
        self.assertIn("result_json", artifact_ids)
        self.assertIn("final_artifact_json", artifact_ids)
        self.assertIn("errors_jsonl", artifact_ids)
        self.assertIn("errors_jsonl_001", artifact_ids)
        self.assertIn("timeline_jsonl", artifact_ids)
        self.assertIn("timeline_json", artifact_ids)
        self.assertIn("observability_summary_json", artifact_ids)
        summary_ref = next(artifact for artifact in listing.json()["artifacts"] if artifact["artifact_id"] == "observability_summary_json")
        self.assertEqual(summary_ref["content_type"], "application/json")
        self.assertIn("/api/v1/runs/artifact-run/artifacts/observability_summary.json", summary_ref["url"])
        self.assertIn("/api/v1/runs/artifact-run/artifacts/observability_summary.json/reveal", summary_ref["reveal_url"])
        self.assertEqual(markdown.status_code, 200)
        self.assertIn("# Draft Review Packet", markdown.text)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.content[:5], b"%PDF-")

    def test_run_artifact_reveal_opens_local_file_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "reveal-run"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({"job_id": "job-reveal"}))

            with (
                patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}),
                patch("mn_api.routes.runs.sys.platform", "darwin"),
                patch("mn_api.routes.runs.subprocess.Popen") as mock_popen,
            ):
                response = self.client.post("/api/v1/runs/reveal-run/artifacts/job.json/reveal")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["path"].endswith("job.json"))
        mock_popen.assert_called_once()
        self.assertEqual(mock_popen.call_args.args[0][:2], ["open", "-R"])
        self.assertTrue(mock_popen.call_args.args[0][2].endswith("job.json"))

    def test_run_outputs_include_recorded_post_launch_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_root = root / "runs"
            run_dir = runs_root / "output-run"
            output_dir = root / "Downloads"
            run_dir.mkdir(parents=True)
            output_dir.mkdir()
            report = output_dir / "output-run-report.md"
            report.write_text("# Customer Report\n")
            (run_dir / "job.json").write_text(json.dumps({"job_id": "job-output"}))
            (run_dir / "post_launch_hook.json").write_text(json.dumps({"ok": True}))
            (run_dir / "post_launch_materialized.json").write_text(json.dumps({
                "ok": True,
                "output_files": [
                    {"kind": "report_markdown", "path": str(report)}
                ],
            }))

            with (
                patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}),
                patch("mn_api.routes.runs.sys.platform", "darwin"),
                patch("mn_api.routes.runs.subprocess.Popen") as mock_popen,
            ):
                artifacts = self.client.get("/api/v1/runs/output-run/artifacts")
                outputs = self.client.get("/api/v1/runs/output-run/outputs")
                downloaded = self.client.get("/api/v1/runs/output-run/outputs/0")
                revealed = self.client.post("/api/v1/runs/output-run/outputs/0/reveal")
                missing = self.client.get("/api/v1/runs/output-run/outputs/99")

        self.assertEqual(artifacts.status_code, 200)
        artifact_ids = {artifact["artifact_id"] for artifact in artifacts.json()["artifacts"]}
        self.assertIn("job_json", artifact_ids)
        self.assertIn("post_launch_hook_json", artifact_ids)
        self.assertIn("output_0_report_markdown", artifact_ids)
        output_ref = next(
            artifact for artifact in artifacts.json()["artifacts"]
            if artifact["artifact_id"] == "output_0_report_markdown"
        )
        self.assertEqual(output_ref["source"], "post_launch_output")
        self.assertTrue(output_ref["external"])
        self.assertEqual(output_ref["name"], "output-run-report.md")
        self.assertEqual(outputs.status_code, 200)
        self.assertEqual(outputs.json()["outputs"][0]["url"], "/api/v1/runs/output-run/outputs/0")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("# Customer Report", downloaded.text)
        self.assertEqual(revealed.status_code, 200)
        self.assertTrue(revealed.json()["path"].endswith("output-run-report.md"))
        self.assertEqual(missing.status_code, 404)
        mock_popen.assert_called_once()

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
    def test_cancel_job_runs_blueprint_post_launch_cleanup(self, mock_client):
        mock_client.cancel_job.return_value = "cancelled"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            run_dir = runs_root / "run-cancel-cleanup"
            bundle_dir = root / "bundle"
            scripts_dir = bundle_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            script_path = scripts_dir / "post-launch.sh"
            script_path.write_text(
                "#!/usr/bin/env bash\n"
                "cat > \"$MN_RUN_DIR/post_cleanup_seen.json\" <<EOF\n"
                "{\n"
                "  \"reason\": \"$MN_POST_LAUNCH_REASON\",\n"
                "  \"run_id\": \"$MN_RUN_ID\",\n"
                "  \"rtsp_port\": \"$RTSP_PORT\",\n"
                "  \"state_file\": \"$MN_POST_LAUNCH_STATE_FILE\",\n"
                "  \"pre_launch_pid\": \"$MN_PRE_LAUNCH_PID\",\n"
                "  \"pre_launch_pgid\": \"$MN_PRE_LAUNCH_PROCESS_GROUP_ID\"\n"
                "}\n"
                "EOF\n"
            )
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-cleanup",
                "run_id": "run-cancel-cleanup",
                "blueprint_id": "worker_one",
            }))
            (run_dir / "pre_launch.ready").write_text(json.dumps({
                "status": "ready",
                "env": {
                    "RTSP_PORT": "8562",
                    "VIDEO_SOURCE_URI": "rtsp://host.openshell.internal:8562/video-watch",
                },
            }))
            (run_dir / "pre_launch_process.json").write_text(json.dumps({
                "pid": 24679,
                "process_group_id": 24680,
            }))
            (run_dir / "post_launch_hook.json").write_text(json.dumps({
                "command": ["bash", str(script_path)],
                "script": str(script_path),
                "cwd": str(bundle_dir),
                "log": str(run_dir / "post_launch.log"),
                "run_id": "run-cancel-cleanup",
                "bundle_dir": str(bundle_dir),
                "state_file": str(run_dir / "post_launch_state.json"),
                "pre_launch_ready_file": str(run_dir / "pre_launch.ready"),
                "pre_launch_process_file": str(run_dir / "pre_launch_process.json"),
            }))

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.post("/api/v1/jobs/job-cleanup/cancel")
                cleanup_record = json.loads((run_dir / "post_cleanup_seen.json").read_text())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "cancelled", "job_id": "job-cleanup"})
        self.assertEqual(cleanup_record["reason"], "job_cancelled")
        self.assertEqual(cleanup_record["run_id"], "run-cancel-cleanup")
        self.assertEqual(cleanup_record["rtsp_port"], "8562")
        self.assertTrue(cleanup_record["state_file"].endswith("post_launch_state.json"))
        self.assertEqual(cleanup_record["pre_launch_pid"], "24679")
        self.assertEqual(cleanup_record["pre_launch_pgid"], "24680")

    @patch('mn_api.state.client')
    def test_blueprint_run_cleans_stale_same_blueprint_hooks_before_start(self, mock_client):
        mock_client.list_jobs.return_value = json.dumps({
            "data": [
                {"job_id": "job-active", "status": "running"},
                {"job_id": "job-stale", "status": "failed"},
            ]
        })
        mock_client.submit_job.return_value = "job-new"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            bundle_dir = repo / "worker_one"
            scripts_dir = bundle_dir / "scripts"
            scripts_dir.mkdir()
            cleanup_script = scripts_dir / "post-launch.sh"
            cleanup_script.write_text(
                "#!/usr/bin/env bash\n"
                "cat > \"$MN_RUN_DIR/cleanup_marker.json\" <<EOF\n"
                "{\n"
                "  \"reason\": \"$MN_POST_LAUNCH_REASON\",\n"
                "  \"run_id\": \"$MN_RUN_ID\"\n"
                "}\n"
                "EOF\n"
            )

            def write_run(run_name, job_id, blueprint_id="worker_one"):
                run_dir = runs_root / run_name
                run_dir.mkdir(parents=True)
                (run_dir / "job.json").write_text(json.dumps({
                    "job_id": job_id,
                    "run_id": run_name,
                    "blueprint_id": blueprint_id,
                }))
                (run_dir / "post_launch_hook.json").write_text(json.dumps({
                    "command": ["bash", str(cleanup_script)],
                    "script": str(cleanup_script),
                    "cwd": str(bundle_dir),
                    "log": str(run_dir / "post_launch.log"),
                    "run_id": run_name,
                    "bundle_dir": str(bundle_dir),
                    "state_file": str(run_dir / "post_launch_state.json"),
                    "pre_launch_ready_file": str(run_dir / "pre_launch.ready"),
                    "pre_launch_process_file": str(run_dir / "pre_launch_process.json"),
                }))
                return run_dir

            stale_run = write_run("stale-run", "job-stale")
            active_run = write_run("active-run", "job-active")
            other_run = write_run("other-run", "job-other", blueprint_id="worker_two")
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    response = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-new"},
                    )
                    cleanup_record = json.loads((stale_run / "cleanup_marker.json").read_text())
                    active_marker_exists = (active_run / "cleanup_marker.json").exists()
                    other_marker_exists = (other_run / "cleanup_marker.json").exists()
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "job-new")
        self.assertEqual(cleanup_record["reason"], "stale_blueprint_start")
        self.assertEqual(cleanup_record["run_id"], "stale-run")
        self.assertFalse(active_marker_exists)
        self.assertFalse(other_marker_exists)

    @patch('mn_api.state.client')
    def test_cancel_job_grpc_error(self, mock_client):
        class MockRpcError(Exception):
            def details(self):
                return "job test_job_123 was not found"
                
        mock_client.cancel_job.side_effect = MockRpcError()
        response = self.client.post("/api/v1/jobs/test_job_123/cancel")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "job test_job_123 was not found"})

    @patch('mn_api.routes.jobs.cleanup_blueprint_processes_for_job')
    @patch('mn_api.state.client')
    def test_cancel_job_runs_blueprint_cleanup_on_backend_error(self, mock_client, mock_cleanup):
        mock_client.cancel_job.side_effect = Exception("backend unavailable")

        response = self.client.post("/api/v1/jobs/test_job_123/cancel")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "backend unavailable"})
        mock_cleanup.assert_called_once_with("test_job_123")

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
    def test_get_job_workflow_progress_uses_grpc_job_and_events(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {
                "job_id": "job-progress",
                "status": "running",
                "submitted_at": "2026-05-31T10:00:00Z",
                "manifest": {
                    "id": "workflow-blueprint",
                    "workflow": {
                        "workflow_id": "workflow-blueprint_v1",
                        "entrypoint": "research",
                        "steps": [{"id": "research", "label": "Research", "run": "research_team"}],
                    },
                    "runtime": {
                        "bindings": {
                            "research_team": {
                                "workers": [{"id": "research:docs", "role": "Analyze docs"}]
                            }
                        }
                    },
                },
            },
            "summary": {"status": "running"},
            "agents": [],
        })
        mock_client.stream_events.return_value = [
            json.dumps({"type": "job_running", "timestamp": "2026-05-31T10:00:01Z"}),
            json.dumps({
                "type": "workflow_step_attempt_completed",
                "timestamp": "2026-05-31T10:00:02Z",
                "payload": {"step": "research", "worker": "research:docs", "tokens": 1200, "tools": 3},
            }),
        ]

        response = self.client.get("/api/v1/jobs/job-progress/workflow-progress")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow_id"], "workflow-blueprint_v1")
        self.assertEqual(body["agent_count"]["done"], 1)
        self.assertEqual(body["agent_count"]["total"], 1)
        self.assertEqual(body["current_step_id"], "research")
        self.assertEqual(body["steps"][0]["agents"][0]["tokens"], 1200)
        mock_client.stream_events.assert_called_once_with("job-progress", follow=False)

    @patch('mn_api.state.client')
    def test_get_job_workflow_progress_enriches_step_and_agent_activity(self, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "activity-run"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-activity",
                "run_id": "activity-run",
                "status": "running",
            }))
            (run_dir / "config.json").write_text(json.dumps({
                "id": "activity-workflow",
                "workflow": {
                    "workflow_id": "activity-workflow_v1",
                    "entrypoint": "research",
                    "steps": [{"id": "research", "label": "Research", "run": "research_team"}],
                },
                "runtime": {
                    "bindings": {
                        "research_team": {
                            "workers": [{"id": "research:docs", "role": "Analyze docs"}]
                        }
                    }
                },
            }))
            persisted_event = {
                "type": "workflow_step_attempt_completed",
                "timestamp": "2026-05-31T10:00:03Z",
                "payload": {
                    "step": "research",
                    "worker": "research:docs",
                    "tokens": 1200,
                    "stdout": "x" * 5000,
                },
            }
            (run_dir / "events.jsonl").write_text(json.dumps(persisted_event) + "\n")
            mock_client.get_job.return_value = json.dumps({
                "job": {
                    "job_id": "job-activity",
                    "run_id": "activity-run",
                    "status": "running",
                    "submitted_at": "2026-05-31T10:00:00Z",
                },
                "summary": {"status": "running"},
                "agents": [],
            })
            mock_client.stream_events.return_value = [
                json.dumps({
                    "type": "workflow_step_attempt_started",
                    "timestamp": "2026-05-31T10:00:01Z",
                    "payload": {"step": "research", "worker": "research:docs", "message": "Reading source documents"},
                }),
            ]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/jobs/job-activity/workflow-progress")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        step = body["steps"][0]
        self.assertEqual(step["last_activity"]["type"], "workflow_step_attempt_completed")
        self.assertEqual(step["last_activity"]["agent_id"], "research:docs")
        self.assertEqual(len(step["recent_events"]), 2)
        self.assertTrue(step["last_activity"]["payload"]["stdout"]["omitted"])
        self.assertEqual(step["activity_summary"], "Agent completed: research:docs")
        agent = step["agents"][0]
        self.assertEqual(agent["last_activity"]["type"], "workflow_step_attempt_completed")
        self.assertEqual(agent["tokens"], 1200)
        self.assertEqual(agent["activity_summary"], "Agent completed: research:docs")

    @patch('mn_api.state.client')
    def test_get_job_events_merges_persisted_run_events_without_duplicates(self, mock_client):
        runtime_event = {
            "type": "job_running",
            "timestamp": "2026-05-31T10:00:01Z",
            "payload": {"run_id": "events-run"},
        }
        persisted_event = {
            "type": "workflow_step_completed",
            "timestamp": "2026-05-31T10:00:02Z",
            "payload": {"run_id": "events-run", "step": "research"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "events-run"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-events",
                "run_id": "events-run",
                "status": "running",
            }))
            (run_dir / "events.jsonl").write_text("\n".join([
                json.dumps(runtime_event),
                json.dumps(persisted_event),
            ]))
            mock_client.stream_events.return_value = [json.dumps(runtime_event)]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/jobs/job-events/events?limit=10")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([event["type"] for event in body["data"]], ["job_running", "workflow_step_completed"])

    @patch('mn_api.state.client')
    def test_get_job_workflow_progress_prefers_run_store_workflow_manifest(self, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "video-run"
            run_dir.mkdir()
            (run_dir / "run.json").write_text(json.dumps({
                "run_id": "video-run",
                "trace_id": "trc_video",
                "status": "running",
            }))
            (run_dir / "observability_summary.json").write_text(json.dumps({
                "schema_version": "mn.observability_summary.v1",
                "run_id": "video-run",
                "trace_id": "trc_video",
                "status": "running",
                "counts": {"events": 2, "logs": 0, "errors": 0, "timeline": 2},
            }))
            (run_dir / "config.json").write_text(json.dumps({
                "id": "video_watch_assistant",
                "type": "service",
                "workflow": {
                    "workflow_id": "video_watch_assistant_v1",
                    "entrypoint": "start_video_monitor",
                    "steps": [
                        {
                            "id": "start_video_monitor",
                            "label": "Start Video Monitor",
                            "run": "start_video_monitor",
                        }
                    ],
                },
                "runtime": {
                    "bindings": {
                        "start_video_monitor": {
                            "workers": [
                                {
                                    "id": "video_monitor",
                                    "alias": "video_monitor",
                                    "display_name": "Video Monitor",
                                    "role": "Coordinate video monitoring",
                                }
                            ]
                        }
                    }
                },
            }))
            (run_dir / "events.jsonl").write_text("\n".join([
                json.dumps({
                    "type": "workflow_step_started",
                    "timestamp": "2026-06-01T10:00:01Z",
                    "payload": {"step": "start_video_monitor"},
                }),
                json.dumps({
                    "type": "workflow_step_attempt_completed",
                    "timestamp": "2026-06-01T10:00:02Z",
                    "payload": {"step": "start_video_monitor", "worker": "video_monitor"},
                }),
            ]))
            mock_client.get_job.return_value = json.dumps({
                "job": {
                    "job_id": "job-progress",
                    "run_id": "video-run",
                    "job_type": "service",
                    "status": "running",
                    "submitted_at": "2026-06-01T10:00:00Z",
                    "manifest": {
                        "graph_id": "runtime-wrapper",
                        "nodes": [{"node_id": "workflow_manifest_executor", "role": "root_coordinator"}],
                    },
                },
                "summary": {"status": "running"},
                "agents": [
                    {
                        "agent_id": "workflow_manifest_executor",
                        "agent_type": "executor",
                        "type": "worker",
                        "status": "paused",
                        "assigned_node": "mirror_neuron@192.168.4.34",
                    }
                ],
            })
            mock_client.stream_events.return_value = [
                json.dumps({"type": "job_running", "timestamp": "2026-06-01T10:00:00Z"}),
            ]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": tmp}):
                response = self.client.get("/api/v1/jobs/job-progress/workflow-progress")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow_id"], "video_watch_assistant_v1")
        self.assertEqual(body["trace_id"], "trc_video")
        self.assertEqual(body["observability_summary"]["trace_id"], "trc_video")
        self.assertEqual(body["steps"][0]["id"], "start_video_monitor")
        self.assertEqual(body["steps"][0]["label"], "Start Video Monitor")
        self.assertEqual(body["steps"][0]["agents"][0]["id"], "video_monitor")
        self.assertEqual(body["steps"][0]["agents"][0]["alias"], "video_monitor")
        self.assertEqual(body["steps"][0]["agents"][0]["display_name"], "Video Monitor")
        self.assertEqual(body["steps"][0]["agents"][0]["assigned_node"], "mirror_neuron@192.168.4.34")

    @patch('mn_api.state.client')
    def test_get_job_workflow_progress_marks_service_workers_idle_from_runtime_topology(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {
                "job_id": "job-progress",
                "job_type": "service",
                "graph_id": "video_watch_assistant_v1",
                "status": "running",
                "submitted_at": "2026-05-31T10:00:00Z",
                "runtime_topology": {
                    "nodes": [
                        {"node_id": "ingress", "agent_type": "router", "type": "map", "role": "root_coordinator"},
                        {"node_id": "visual_detector", "agent_type": "executor", "type": "stream", "role": "visual_detector"},
                    ]
                },
            },
            "summary": {"status": "running"},
            "agents": [],
        })
        mock_client.stream_events.return_value = [
            json.dumps({"type": "job_running", "timestamp": "2026-05-31T10:00:00Z"}),
            json.dumps({"type": "route_selected", "timestamp": "2026-05-31T10:00:01Z", "agent_id": "ingress"}),
            json.dumps({"type": "executor_lease_acquired", "timestamp": "2026-05-31T10:00:02Z", "agent_id": "visual_detector"}),
            json.dumps({"type": "sandbox_job_completed", "timestamp": "2026-05-31T10:00:05Z", "agent_id": "visual_detector"}),
        ]

        response = self.client.get("/api/v1/jobs/job-progress/workflow-progress")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow_kind"], "service")
        self.assertEqual(body["current_step_id"], "visual_detector")
        self.assertEqual(body["current_step"]["status"], "idle")
        self.assertEqual(body["steps"][0]["status"], "done")
        self.assertEqual(body["agent_count"]["ready"], 2)

    @patch('mn_api.state.client')
    def test_stream_job_workflow_progress_emits_snapshots(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {
                "job_id": "job-progress",
                "status": "running",
                "submitted_at": "2026-05-31T10:00:00Z",
                "manifest": {
                    "id": "workflow-blueprint",
                    "workflow": {
                        "workflow_id": "workflow-blueprint_v1",
                        "entrypoint": "research",
                        "steps": [{"id": "research", "label": "Research", "run": "research_team"}],
                    },
                    "runtime": {
                        "bindings": {
                            "research_team": {"workers": [{"id": "research:docs", "role": "Analyze docs"}]}
                        }
                    },
                },
            },
            "summary": {"status": "running"},
            "agents": [],
        })
        history_event = json.dumps({
            "type": "workflow_step_started",
            "timestamp": "2026-05-31T10:00:01Z",
            "payload": {"step": "research"},
        })
        mock_client.stream_events.side_effect = [
            [history_event],
            [
                history_event,
                json.dumps({
                    "type": "workflow_step_attempt_completed",
                    "timestamp": "2026-05-31T10:00:02Z",
                    "payload": {"step": "research", "worker": "research:docs"},
                }),
                json.dumps({"type": "job_completed", "timestamp": "2026-05-31T10:00:03Z"}),
            ],
        ]

        with self.client.stream("GET", "/api/v1/jobs/job-progress/workflow-progress/stream") as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: snapshot", body)
        self.assertIn('"status": "completed"', body)
        self.assertIn('"done": 1', body)

    @patch('mn_api.state.client')
    def test_get_job_defaults_to_compact_artifact_refs_for_large_runs(self, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "compact-run"
            output_dir = runs_root / "external-outputs"
            run_dir.mkdir()
            output_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-large",
                "run_id": "compact-run",
                "graph_id": "personal_income_tax_expert_v1",
                "status": "completed",
            }))
            (run_dir / "run.json").write_text(json.dumps({
                "run_id": "compact-run",
                "trace_id": "trc_compact",
                "status": "completed",
            }))
            (run_dir / "observability_summary.json").write_text(json.dumps({
                "schema_version": "mn.observability_summary.v1",
                "run_id": "compact-run",
                "trace_id": "trc_compact",
                "status": "completed",
                "counts": {"events": 1, "logs": 0, "errors": 0, "timeline": 1},
            }))
            (run_dir / "result.json").write_text(json.dumps({"ok": True}))
            (run_dir / "final_artifact.json").write_text(json.dumps({
                "type": "prepared_1040_tax_packet",
                "advisor_message": "Draft packet.",
            }))
            (run_dir / "report.md").write_text("# Draft Review Packet\n")
            (run_dir / "tax-review-packet.pdf").write_bytes(b"%PDF-1.4\n")
            external_report = output_dir / "compact-run-report.md"
            external_report.write_text("# Customer Output\n")
            (run_dir / "post_launch_materialized.json").write_text(json.dumps({
                "ok": True,
                "output_files": [
                    {"kind": "report_markdown", "path": str(external_report)}
                ],
            }))

            huge_log = "x" * (5 * 1024 * 1024)
            mock_client.get_job.side_effect = AssertionError("default job details should not call full gRPC get_job")
            mock_client.stream_events.return_value = [
                json.dumps({
                    "type": "job_completed",
                    "timestamp": "2026-05-25T23:00:00Z",
                    "agent_id": "tax_manager",
                    "sandbox": {"logs": huge_log},
                    "payload": {
                        "run_id": "compact-run",
                        "result": {"final_artifact": {"body": huge_log}},
                    },
                })
            ]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/jobs/job-large")

        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 4 * 1024 * 1024)
        self.assertNotIn(huge_log[:1024], response.text)
        body = response.json()
        self.assertEqual(body["job"]["run_id"], "compact-run")
        self.assertEqual(body["job"]["trace_id"], "trc_compact")
        self.assertEqual(body["observability_summary"]["trace_id"], "trc_compact")
        self.assertEqual(body["job"]["status"], "completed")
        self.assertEqual(body["summary"]["mode"], "compact")
        artifact_ids = {artifact["artifact_id"] for artifact in body["artifacts"]}
        self.assertIn("result_json", artifact_ids)
        self.assertIn("final_artifact_json", artifact_ids)
        self.assertIn("output_0_report_markdown", artifact_ids)
        self.assertTrue(any(artifact["content_type"] == "application/pdf" for artifact in body["artifacts"]))
        output_ref = next(artifact for artifact in body["output_files"] if artifact["artifact_id"] == "output_0_report_markdown")
        self.assertEqual(output_ref["source"], "post_launch_output")
        self.assertEqual(output_ref["url"], "/api/v1/runs/compact-run/outputs/0")

    @patch('mn_api.state.client')
    def test_get_job_compact_upgrades_legacy_failure_reason(self, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "failed-run"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-failed",
                "run_id": "failed-run",
                "graph_id": "failure_graph",
                "status": "failed",
            }))
            (run_dir / "errors.jsonl").write_text("")
            mock_client.get_job.side_effect = AssertionError("default job details should not call full gRPC get_job")
            mock_client.stream_events.return_value = [
                json.dumps({
                    "type": "job_failed",
                    "timestamp": "2026-06-04T12:00:00Z",
                    "reason": "workflow step heartbeat deadline exceeded",
                    "payload": {"run_id": "failed-run", "step_id": "prepare"},
                })
            ]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/jobs/job-failed")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["failure"]["schema_version"], "mn.error.v1")
        self.assertEqual(body["failure"]["desc"], "Workflow step heartbeat deadline exceeded")
        self.assertEqual(body["summary"]["failure"]["code"], "runtime.failure")
        self.assertEqual(body["job"]["reason"] if "reason" in body["job"] else body["failure"]["desc"], "Workflow step heartbeat deadline exceeded")

    @patch('mn_api.state.client')
    def test_get_job_compact_suppresses_failure_for_completed_job(self, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "completed-run"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-completed",
                "run_id": "completed-run",
                "graph_id": "invoice_bill_extraction_assistant_v1",
                "status": "completed",
            }))
            (run_dir / "run.json").write_text(json.dumps({
                "run_id": "completed-run",
                "status": "completed",
            }))
            mock_client.get_job.side_effect = AssertionError("default job details should not call full gRPC get_job")
            mock_client.stream_events.return_value = [
                json.dumps({
                    "type": "job_running",
                    "timestamp": "2026-06-04T12:00:00Z",
                    "payload": {"run_id": "completed-run"},
                }),
                json.dumps({
                    "type": "job_failed",
                    "timestamp": "2026-06-04T12:00:01Z",
                    "reason": "fewer than 2 healthy connected runtime nodes observed",
                    "payload": {"run_id": "completed-run"},
                }),
                json.dumps({
                    "type": "job_completed",
                    "timestamp": "2026-06-04T12:00:02Z",
                    "payload": {"run_id": "completed-run"},
                }),
            ]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/jobs/job-completed")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["job"]["status"], "completed")
        self.assertIsNone(body["failure"])
        self.assertNotIn("failure", body["job"])
        self.assertNotIn("failure", body["summary"])
        self.assertTrue(any(event.get("failure") for event in body["events"]))

    @patch('mn_api.state.client')
    def test_get_job_compact_does_not_promote_reliability_reason_to_failure(self, mock_client):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp)
            run_dir = runs_root / "running-run"
            run_dir.mkdir()
            (run_dir / "job.json").write_text(json.dumps({
                "job_id": "job-running",
                "run_id": "running-run",
                "graph_id": "invoice_bill_extraction_assistant_v1",
                "status": "running",
            }))
            mock_client.get_job.side_effect = AssertionError("default job details should not call full gRPC get_job")
            mock_client.stream_events.return_value = [
                json.dumps({
                    "type": "reliability_strategy_resolved",
                    "timestamp": "2026-06-04T12:00:00Z",
                    "requested_recovery_policy": "auto",
                    "effective_recovery_policy": "local_restart",
                    "mode": "single_node",
                    "degraded": False,
                    "reason": "fewer than 2 healthy connected runtime nodes observed",
                    "observed_nodes": ["mirror_neuron@127.0.0.1"],
                }),
                json.dumps({
                    "type": "job_running",
                    "timestamp": "2026-06-04T12:00:01Z",
                    "payload": {"run_id": "running-run"},
                }),
            ]

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(runs_root)}):
                response = self.client.get("/api/v1/jobs/job-running")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["job"]["status"], "running")
        self.assertIsNone(body["failure"])
        self.assertNotIn("failure", body["job"])
        self.assertNotIn("failure", body["summary"])
        self.assertFalse(any(event.get("failure") for event in body["events"]))

    @patch('mn_api.state.client')
    def test_get_job_workflow_progress_suppresses_failure_for_completed_job(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {
                "job_id": "job-completed",
                "status": "completed",
                "manifest": {
                    "id": "invoice-blueprint",
                    "workflow": {
                        "workflow_id": "invoice-blueprint_v1",
                        "entrypoint": "extract",
                        "steps": [{"id": "extract", "label": "Extract", "run": "extractor"}],
                    },
                    "runtime": {
                        "bindings": {
                            "extractor": {"workers": [{"id": "extractor", "role": "Extract fields"}]}
                        }
                    },
                },
            },
            "summary": {"status": "completed"},
            "agents": [],
        })
        mock_client.stream_events.return_value = [
            json.dumps({
                "type": "workflow_step_attempt_completed",
                "timestamp": "2026-06-04T12:00:00Z",
                "payload": {"step": "extract", "worker": "extractor"},
            }),
            json.dumps({
                "type": "job_failed",
                "timestamp": "2026-06-04T12:00:01Z",
                "reason": "fewer than 2 healthy connected runtime nodes observed",
            }),
            json.dumps({"type": "job_completed", "timestamp": "2026-06-04T12:00:02Z"}),
        ]

        response = self.client.get("/api/v1/jobs/job-completed/workflow-progress")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertNotIn("failure", body)

    @patch('mn_api.state.client')
    def test_get_job_compact_does_not_treat_agent_completion_as_job_completion(self, mock_client):
        mock_client.get_job.side_effect = AssertionError("default job details should not call full gRPC get_job")
        mock_client.stream_events.return_value = [
            json.dumps({
                "type": "job_started",
                "timestamp": "2026-05-25T23:00:00Z",
                "payload": {"run_id": "service-run"},
            }),
            json.dumps({
                "type": "sandbox_job_completed",
                "timestamp": "2026-05-25T23:00:05Z",
                "agent_id": "visual_detector",
                "payload": {"exit_code": 0, "run_id": "service-run"},
            }),
        ]

        response = self.client.get("/api/v1/jobs/service-job")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["job"]["status"], "running")
        self.assertEqual(body["summary"]["status"], "running")

    @patch('mn_api.state.client')
    def test_get_job_compact_infers_lifecycle_status_from_bare_runtime_events(self, mock_client):
        mock_client.get_job.side_effect = AssertionError("default job details should not call full gRPC get_job")
        cases = {
            "job_pausing": "pausing",
            "job_paused": "paused",
            "job_resumed": "running",
            "job_cancelled": "cancelled",
        }

        for event_type, expected_status in cases.items():
            with self.subTest(event_type=event_type):
                mock_client.stream_events.return_value = [
                    json.dumps({
                        "type": event_type,
                        "timestamp": "2026-06-04T12:00:00Z",
                        "payload": {"run_id": "lifecycle-run"},
                    })
                ]

                response = self.client.get(f"/api/v1/jobs/{event_type}-job")

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["job"]["status"], expected_status)
                self.assertEqual(body["summary"]["status"], expected_status)

    @patch('mn_api.state.client')
    def test_get_job_include_full_keeps_debug_grpc_path(self, mock_client):
        mock_client.get_job.return_value = json.dumps({
            "job": {"job_id": "job-full", "graph_id": "graph-1", "status": "running"}
        })

        response = self.client.get("/api/v1/jobs/job-full?include=full")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job"]["job_id"], "job-full")
        mock_client.get_job.assert_called_once_with("job-full")

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
    def test_replay_job_dead_letter_reports_not_exposed_without_backend_call(self, mock_client):
        response = self.client.post("/api/v1/jobs/test_job_123/dead-letters/2/replay")

        self.assertEqual(response.status_code, 501)
        body = response.json()["detail"]
        self.assertEqual(body["error"], "dead_letter_replay_not_exposed")
        self.assertEqual(body["job_id"], "test_job_123")
        self.assertEqual(body["index"], 2)
        mock_client.stream_events.assert_not_called()

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

    @patch('mn_api.state.client')
    def test_pause_and_resume_job_grpc_errors(self, mock_client):
        class MockRpcError(Exception):
            def __init__(self, detail):
                self.detail = detail

            def details(self):
                return self.detail

        mock_client.pause_job.side_effect = MockRpcError("job test_job_123 cannot be paused")
        pause_response = self.client.post("/api/v1/jobs/test_job_123/pause")
        self.assertEqual(pause_response.status_code, 500)
        self.assertEqual(pause_response.json(), {"error": "job test_job_123 cannot be paused"})

        mock_client.resume_job.side_effect = MockRpcError("job test_job_123 cannot be resumed")
        resume_response = self.client.post("/api/v1/jobs/test_job_123/resume")
        self.assertEqual(resume_response.status_code, 500)
        self.assertEqual(resume_response.json(), {"error": "job test_job_123 cannot be resumed"})

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
                with patch.dict(
                    'os.environ',
                    {
                        "MN_RUNS_ROOT": str(runs_root),
                        "MN_SHARED_STORAGE_ROOT": str(repo / "shared"),
                        "MN_RUNTIME_SHARED_STORAGE_ROOT": str(repo / "shared"),
                    },
                ):
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
        self.assertTrue(env["MN_RUNS_ROOT"].startswith(str(repo / "shared" / "submissions" / "run-123-")))
        self.assertTrue(env["MN_RUNS_ROOT"].endswith("/outputs/runs"))
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(injected_config["identity"]["run_id"], "run-123")
        self.assertEqual(injected_config["outputs"]["run_root"], env["MN_RUNS_ROOT"])
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
    def test_blueprint_run_starts_event_relay_for_post_launch_batch_worker(self, mock_client):
        mock_client.submit_job.return_value = "job-post-launch"
        mock_client.list_jobs.return_value = json.dumps({"data": []})
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            scripts_dir = repo / "worker_one" / "scripts"
            scripts_dir.mkdir()
            post_launch_script = scripts_dir / "post-launch.sh"
            post_launch_script.write_text("#!/usr/bin/env bash\n")
            popen_commands = []

            def fake_popen(command, **_kwargs):
                popen_commands.append(command)
                return SimpleNamespace(pid=55555)

            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    with patch('mn_api.blueprints.subprocess.Popen', side_effect=fake_popen):
                        response = self.client.post(
                            "/api/v1/blueprints/worker_one/runs",
                            json={"run_id": "run-post-launch"},
                        )
                        relay_info = json.loads((runs_root / "run-post-launch" / "event_relay.json").read_text())
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        relay_commands = [command for command in popen_commands if "mn_blueprint_support.event_relay" in command]
        self.assertEqual(len(relay_commands), 1)
        self.assertEqual(relay_commands[0][:9], [
            sys.executable,
            "-m",
            "mn_blueprint_support.event_relay",
            "--job-id",
            "job-post-launch",
            "--run-dir",
            str(runs_root / "run-post-launch"),
            "--poll-seconds",
            "1",
        ])
        self.assertEqual(relay_info["job_id"], "job-post-launch")
        self.assertEqual(relay_info["run_id"], "run-post-launch")
        self.assertEqual(relay_info["service"], {})

    @patch('mn_api.state.client')
    def test_blueprint_run_reads_latest_local_repo_config(self, mock_client):
        mock_client.submit_job.return_value = "job-local-config"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text(json.dumps({
                "graph_id": "worker_one_graph",
                "nodes": [
                    {
                        "node_id": "worker",
                        "config": {"environment": {}},
                    }
                ],
                "edges": [],
                "metadata": {},
            }))
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "vl_model": {"model": "edited-local-model"}
            }))
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    response = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-local-config"},
                    )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        manifest_json, _payloads = mock_client.submit_job.call_args.args
        submitted_env = json.loads(manifest_json)["nodes"][0]["config"]["environment"]
        submitted_config = json.loads(submitted_env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(submitted_config["vl_model"]["model"], "edited-local-model")

    @patch('mn_api.state.client')
    def test_blueprint_run_starts_pre_launch_before_submit(self, mock_client):
        mock_client.submit_job.return_value = "job-pre-launch"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            (repo / "worker_one" / "manifest.json").write_text(json.dumps({
                "graph_id": "worker_one_graph",
                "nodes": [
                    {
                        "node_id": "worker",
                        "config": {"environment": {}},
                    }
                ],
                "edges": [],
                "metadata": {},
                "input_validation": {
                    "rules": [
                        {
                            "name": "pre_launch_env",
                            "type": "command",
                            "command": [sys.executable, "payloads/check_pre_launch_env.py"],
                        }
                    ]
                },
            }))
            (repo / "worker_one" / "payloads" / "check_pre_launch_env.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "value = os.environ.get('VIDEO_SOURCE_URI', '')\n"
                "Path('validation_env.txt').write_text(value)\n"
                "raise SystemExit(0 if value == 'rtsp://host.openshell.internal:8562/video-watch' else 1)\n"
            )
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
            pre_launch_commands = []
            real_popen = __import__("subprocess").Popen

            def fake_popen(_command, **kwargs):
                if _command != ["bash", str(script_path.resolve())]:
                    return real_popen(_command, **kwargs)
                pre_launch_commands.append(_command)
                captured_env.update(kwargs["env"])
                Path(kwargs["env"]["MN_PRE_LAUNCH_READY_FILE"]).write_text(json.dumps({
                    "status": "ready",
                    "env": {
                        "RTSP_PORT": "8562",
                        "STREAM_URI": "rtsp://127.0.0.1:8562/video-watch",
                        "VIDEO_SOURCE_URI": "rtsp://host.openshell.internal:8562/video-watch",
                    },
                    "config": {
                        "video_source": {"uri": "rtsp://127.0.0.1:8562/video-watch"},
                        "web_ui": {"dashboard": {"default_video_source": "rtsp://127.0.0.1:8562/video-watch"}},
                    },
                }))
                return process

            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    with patch('mn_api.blueprints.subprocess.Popen', side_effect=fake_popen):
                        response = self.client.post(
                            "/api/v1/blueprints/worker_one/runs",
                            json={"run_id": "run-pre-launch"},
                        )
                        process_info = json.loads((runs_root / "run-pre-launch" / "pre_launch_process.json").read_text())
                        validation_env = (repo / "worker_one" / "validation_env.txt").read_text()
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "job-pre-launch")
        self.assertEqual(pre_launch_commands, [["bash", str(script_path.resolve())]])
        self.assertEqual(captured_env["MN_RUN_ID"], "run-pre-launch")
        self.assertEqual(captured_env["MN_BLUEPRINT_BUNDLE_DIR"], str((repo / "worker_one").resolve()))
        self.assertEqual(json.loads(captured_env["MN_BLUEPRINT_CONFIG_JSON"])["identity"]["run_id"], "run-pre-launch")
        submitted_manifest = json.loads(mock_client.submit_job.call_args.args[0])
        submitted_env = submitted_manifest["nodes"][0]["config"]["environment"]
        self.assertEqual(submitted_env["VIDEO_SOURCE_URI"], "rtsp://host.openshell.internal:8562/video-watch")
        self.assertEqual(validation_env, "rtsp://host.openshell.internal:8562/video-watch")
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

    @patch("mn_api.routes.blueprints.run_mn_blueprint_validate")
    def test_blueprint_launch_validate_uses_mn_cli_for_catalog_blueprint(self, mock_validate):
        mock_validate.return_value = {"ok": True, "status": "passed", "issues": [], "errors": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post(
                    "/api/v1/blueprints/launch/validate",
                    json={"source": "catalog", "blueprint_id": "worker_one"},
                )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "catalog")
        self.assertEqual(body["blueprint"]["id"], "worker_one")
        self.assertTrue(body["validation"]["ok"])
        mock_validate.assert_called_once()
        self.assertEqual(mock_validate.call_args.args[0].name, "worker_one")

    @patch("mn_api.routes.blueprints.run_mn_blueprint_run")
    @patch("mn_api.routes.blueprints.run_mn_blueprint_validate")
    def test_blueprint_launch_run_uses_mn_cli_for_catalog_blueprint(self, mock_validate, mock_run):
        mock_validate.return_value = {"ok": True, "status": "passed", "issues": [], "errors": []}
        mock_run.return_value = {"ok": True, "job_id": "job-from-cli", "run_id": "run-from-cli", "command": "mn blueprint run"}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post(
                    "/api/v1/blueprints/launch/runs",
                    json={"source": "catalog", "blueprint_id": "worker_one"},
                )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["job_id"], "job-from-cli")
        self.assertEqual(body["id"], "job-from-cli")
        self.assertEqual(body["run_id"], "run-from-cli")
        self.assertEqual(mock_run.call_args.args[0], ["--folder", str((repo / "worker_one").resolve()), "--detached"])

    @patch("mn_api.routes.blueprints.run_mn_blueprint_run")
    @patch("mn_api.routes.blueprints.run_mn_blueprint_validate")
    @patch("mn_api.routes.blueprints.install_blueprint_runtime_models")
    def test_blueprint_launch_run_installs_models_before_validation(self, mock_install, mock_validate, mock_run):
        events = []

        def install_side_effect(*args, **kwargs):
            events.append("install")
            self.assertFalse(kwargs["force"])
            return {
                "ok": True,
                "models": [{"id": "gemma4:e2b", "model": "ai/gemma4:E2B", "status": "installed"}],
                "errors": [],
            }

        def validate_side_effect(*args, **kwargs):
            events.append("validate")
            return {"ok": True, "status": "passed", "issues": [], "errors": []}

        mock_install.side_effect = install_side_effect
        mock_validate.side_effect = validate_side_effect
        mock_run.return_value = {"ok": True, "job_id": "job-from-cli", "run_id": "run-from-cli", "command": "mn blueprint run"}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict(os.environ, {"MN_LAUNCH_PROGRESS_DIR": str(repo / "progress")}):
                    response = self.client.post(
                        "/api/v1/blueprints/launch/runs",
                        json={
                            "source": "catalog",
                            "blueprint_id": "worker_one",
                            "progress_id": "launch-api-test",
                        },
                    )
                    progress_response = self.client.get("/api/v1/blueprints/launch/progress/launch-api-test")
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["install", "validate"])
        body = response.json()
        self.assertEqual(body["progress_id"], "launch-api-test")
        self.assertEqual(body["model_install"]["models"][0]["status"], "installed")
        self.assertEqual(progress_response.status_code, 200)
        progress_body = progress_response.json()
        self.assertTrue(progress_body["completed"])
        progress_phases = [event["phase"] for event in progress_body["events"]]
        self.assertIn("model_install", progress_phases)
        self.assertIn("validation", progress_phases)
        self.assertEqual(progress_body["events"][-1]["phase"], "launch")
        self.assertEqual(progress_body["events"][-1]["status"], "completed")
        mock_run.assert_called_once()

    def test_blueprint_launch_progress_rejects_invalid_progress_id(self):
        response = self.client.get("/api/v1/blueprints/launch/progress/not/valid")
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/api/v1/blueprints/launch/progress/bad%20id")
        self.assertEqual(response.status_code, 400)

    @patch("mn_api.routes.blueprints.run_mn_blueprint_run")
    @patch("mn_api.routes.blueprints.run_mn_blueprint_validate")
    @patch("mn_api.routes.blueprints.install_blueprint_runtime_models")
    def test_blueprint_launch_run_blocks_when_auto_model_install_fails(self, mock_install, mock_validate, mock_run):
        mock_install.return_value = {
            "ok": False,
            "models": [
                {
                    "id": "gemma4:e2b",
                    "model": "ai/gemma4:E2B",
                    "path": "llm.runtime_model",
                    "status": "failed",
                    "error": "hardware is not compatible",
                }
            ],
            "errors": ["hardware is not compatible"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post(
                    "/api/v1/blueprints/launch/runs",
                    json={"source": "catalog", "blueprint_id": "worker_one"},
                )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.json()["error"], "blueprint_model_install_failed")
        self.assertEqual(response.json()["errors"][0]["location"]["path"], "llm.runtime_model")
        mock_validate.assert_not_called()
        mock_run.assert_not_called()

    @patch("mn_api.routes.blueprints.run_mn_blueprint_run")
    @patch("mn_api.routes.blueprints.run_mn_blueprint_validate")
    def test_blueprint_launch_run_uses_mn_cli_for_filesystem_path(self, mock_validate, mock_run):
        mock_validate.return_value = {"ok": True, "status": "passed", "issues": [], "errors": []}
        mock_run.return_value = {"ok": True, "job_id": "job-path-cli", "command": "mn blueprint run --folder"}
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_dir = Path(tmpdir) / "local_worker"
            (bundle_dir / "payloads").mkdir(parents=True)
            (bundle_dir / "manifest.json").write_text(json.dumps({
                "graph_id": "local_worker_graph",
                "job_name": "Local Worker",
                "nodes": [],
                "edges": [],
            }))
            response = self.client.post(
                "/api/v1/blueprints/launch/runs",
                json={"source": "path", "path": str(bundle_dir)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "job-path-cli")
        self.assertEqual(mock_run.call_args.args[0], ["--folder", str(bundle_dir.resolve()), "--detached"])

    @patch("mn_api.routes.blueprints.run_mn_blueprint_run")
    @patch("mn_api.routes.blueprints.run_mn_blueprint_validate")
    def test_blueprint_launch_validation_failure_blocks_cli_run(self, mock_validate, mock_run):
        mock_validate.return_value = {
            "ok": False,
            "status": "failed",
            "errors": ["bad input"],
            "issues": [{"message": "bad input"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                response = self.client.post(
                    "/api/v1/blueprints/launch/runs",
                    json={"source": "catalog", "blueprint_id": "worker_one"},
                )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "blueprint_validation_failed")
        mock_run.assert_not_called()

    def test_blueprint_launch_cli_field_parser_reads_rich_output(self):
        output = """
        ╭──────────────────── Job submitted successfully ────────────────────╮
        │ Bundle           video_watch_assistant                             │
        │ Job ID           vwav-755a88af                                     │
        │ Blueprint Run ID video_watch_assistant-20260531T224413Z-412c9cb2f1 │
        ╰────────────────────────────────────────────────────────────────────╯
        """

        self.assertEqual(parse_cli_field(output, "Job ID"), "vwav-755a88af")
        self.assertEqual(
            parse_cli_field(output, "Blueprint Run ID"),
            "video_watch_assistant-20260531T224413Z-412c9cb2f1",
        )

    @patch('mn_api.state.client')
    @patch("mn_api.routes.blueprints.validate_blueprint_inputs")
    @patch("mn_api.routes.blueprints.install_blueprint_runtime_models")
    def test_blueprint_run_installs_models_before_input_validation(self, mock_install, mock_validate, mock_client):
        events = []

        def install_side_effect(*args, **kwargs):
            events.append("install")
            return {
                "ok": True,
                "models": [{"id": "gemma4:e2b", "model": "ai/gemma4:E2B", "status": "installed"}],
                "errors": [],
            }

        def validate_side_effect(*args, **kwargs):
            events.append("validate")
            return {"ok": True, "status": "passed", "issues": [], "errors": []}

        mock_install.side_effect = install_side_effect
        mock_validate.side_effect = validate_side_effect
        mock_client.submit_job.return_value = "job-with-model"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
            self._write_blueprint_repo(repo)
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    response = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-with-auto-model"},
                    )
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ["install", "validate"])
        self.assertEqual(response.json()["model_install"]["models"][0]["status"], "installed")
        self.assertEqual(response.json()["job_id"], "job-with-model")

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
        self.assertEqual(failed.json()["error"], "blueprint_validation_failed")
        self.assertEqual(failed.json()["errors"][0]["location"]["path"], "llm.api_base")
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.json()["job_id"], "job-forced")
        manifest_json, _payloads = mock_client.submit_job.call_args.args
        self.assertTrue(json.loads(manifest_json)["metadata"]["mn_validation"]["force"])
        self.assertEqual(json.loads(manifest_json)["metadata"]["mn_validation"]["status"], "skipped")
        self.assertTrue(mock_client.submit_job.call_args.kwargs["force"])

    @patch('mn_api.state.client')
    def test_blueprint_run_validation_failure_runs_post_launch_cleanup(self, mock_client):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            runs_root = (repo / "runs").resolve()
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
            scripts_dir = repo / "worker_one" / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "pre-launch.sh").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'ready\\n' > \"$MN_PRE_LAUNCH_READY_FILE\"\n"
                "while true; do sleep 1; done\n"
            )
            (scripts_dir / "post-launch.sh").write_text(
                "#!/usr/bin/env bash\n"
                "cat > \"$MN_RUN_DIR/post_cleanup_seen.json\" <<EOF\n"
                "{\n"
                "  \"reason\": \"$MN_POST_LAUNCH_REASON\",\n"
                "  \"run_id\": \"$MN_RUN_ID\",\n"
                "  \"ready_file\": \"$MN_PRE_LAUNCH_READY_FILE\",\n"
                "  \"process_file\": \"$MN_PRE_LAUNCH_PROCESS_FILE\",\n"
                "  \"state_file\": \"$MN_POST_LAUNCH_STATE_FILE\"\n"
                "}\n"
                "EOF\n"
            )
            config_dir = repo / "worker_one" / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(json.dumps({
                "llm": {"api_base": "not-a-url"}
            }))
            original = self._set_blueprint_config(repo)
            try:
                with patch.dict('os.environ', {"MN_RUNS_ROOT": str(runs_root)}):
                    response = self.client.post(
                        "/api/v1/blueprints/worker_one/runs",
                        json={"run_id": "run-post-cleanup"},
                    )
                    cleanup_record = json.loads((runs_root / "run-post-cleanup" / "post_cleanup_seen.json").read_text())
            finally:
                self._restore_config(original)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "blueprint_validation_failed")
        self.assertEqual(cleanup_record["reason"], "validation_failed")
        self.assertEqual(cleanup_record["run_id"], "run-post-cleanup")
        self.assertTrue(cleanup_record["ready_file"].endswith("pre_launch.ready"))
        self.assertTrue(cleanup_record["process_file"].endswith("pre_launch_process.json"))
        self.assertTrue(cleanup_record["state_file"].endswith("post_launch_state.json"))
        mock_client.submit_job.assert_not_called()

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
