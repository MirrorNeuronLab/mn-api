import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from mn_sdk import add_registered_models, provider_registration, set_registered_default_model

import mn_api.blueprints as blueprints_module
from mn_api import state
from mn_api.blueprints import (
    blueprint_requires_context_engine,
    blueprint_bundle_root,
    cached_git_repo_path,
    cleanup_blueprint_run_processes,
    cleanup_run_process,
    defer_blueprint_runtime_models,
    ensure_git_blueprint_repo,
    filter_blueprints_by_category,
    install_blueprint_runtime_models,
    is_git_repo_url,
    load_blueprint_categories,
    load_blueprint_bundle,
    load_blueprint_catalog,
    model_match_keys,
    model_service_tags,
    runtime_blueprint_environment_overrides,
    validate_run_id,
)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _port_accepts_connection(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _single_node_resource_report(node_name: str = "test-runtime-node") -> str:
    return json.dumps(
        {
            "nodes": [
                {
                    "name": node_name,
                    "status": "healthy",
                    "scheduling_eligible": True,
                    "self": True,
                    "coordination_store": {
                        "identity": "test-store",
                        "writable_primary": True,
                        "healthy": True,
                    },
                }
            ]
        }
    )


class TestBlueprintServices(unittest.TestCase):
    @patch("mn_api.blueprints.runtime_process_environment", return_value={"PATH": "/docker"})
    @patch(
        "mn_api.blueprints.package_payload_models",
        return_value=[
            {
                "id": "primary",
                "model": "demo/payload:latest",
                "status": "packaged",
            }
        ],
    )
    def test_package_payload_models_for_api_uses_runtime_environment(
        self,
        mock_package,
        _mock_environment,
    ):
        manifest = {"runtime": {"models": {}}}

        result = blueprints_module.package_payload_models_for_api(
            Path("/bundle"),
            manifest,
        )

        self.assertEqual(result[0]["status"], "packaged")
        mock_package.assert_called_once_with(
            Path("/bundle"),
            manifest,
            env={"PATH": "/docker"},
        )

    def test_model_service_tags_include_nemotron_aliases(self):
        entry = {
            "id": "nemotron-3.5-lightning:latest",
            "model": "nemotron-3.5-lightning:latest",
            "api_model": "nemotron-3.5-lightning:latest",
            "aliases": ["nemotron-3.5-lightning"],
        }

        tags = model_service_tags(entry)

        self.assertIn("model:nemotron-3.5-lightning:latest", tags)
        self.assertIn("model:nemotron-3.5-lightning", tags)
        self.assertIn("model-id:nemotron-3.5-lightning:latest", tags)
        self.assertIn("model-id:nemotron-3.5-lightning", tags)
        self.assertIn("nemotron-3.5-lightning", model_match_keys("nemotron-3.5-lightning:latest"))

    def test_is_git_repo_url_accepts_common_remote_forms(self):
        self.assertTrue(is_git_repo_url("https://github.com/MirrorNeuronLab/otterdesk-blueprints.git"))
        self.assertTrue(is_git_repo_url("ssh://git@github.com/MirrorNeuronLab/otterdesk-blueprints.git"))
        self.assertTrue(is_git_repo_url("git@github.com:MirrorNeuronLab/otterdesk-blueprints.git"))
        self.assertFalse(is_git_repo_url("/tmp/mirror-neuron-set/otterdesk-blueprints"))

    def test_runtime_blueprint_environment_overrides_reads_persisted_web_ui_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mn_home = Path(tmpdir) / ".mn"
            mn_home.mkdir()
            (mn_home / "docker-compose.env").write_text(
                "\n".join(
                    [
                        "MN_BLUEPRINT_WEB_UI_BIND_HOST=0.0.0.0",
                        "MN_BLUEPRINT_WEB_UI_PUBLIC_HOST=localhost",
                        "MN_BLUEPRINT_WEB_UI_PORT_START=61000",
                        "MN_BLUEPRINT_WEB_UI_PORT_END=61049",
                        "MN_BLUEPRINT_WEB_UI_PORT_ALLOCATION_MODE=prepublished",
                    ]
                )
                + "\n"
            )

            with patch.dict(os.environ, {"HOME": tmpdir}, clear=True):
                overrides = runtime_blueprint_environment_overrides()

        self.assertEqual(overrides["MN_BLUEPRINT_WEB_UI_BIND_HOST"], "0.0.0.0")
        self.assertEqual(overrides["MN_BLUEPRINT_WEB_UI_PUBLIC_HOST"], "localhost")
        self.assertEqual(overrides["MN_BLUEPRINT_WEB_UI_PORT_START"], "61000")
        self.assertEqual(overrides["MN_BLUEPRINT_WEB_UI_PORT_END"], "61049")
        self.assertEqual(overrides["MN_BLUEPRINT_WEB_UI_PORT_ALLOCATION_MODE"], "prepublished")

    def test_runtime_path_environment_uses_cli_style_process_env_and_docker_path(self):
        observed = {}

        def fake_sdk_runtime_path_environment(*, env=None, workspace_root=None):
            observed["sdk_env"] = dict(env or {})
            observed["workspace_root"] = workspace_root
            return {"PYTHONPATH": "/runtime/python"}

        def fake_docker_cli_path_environment(env=None):
            observed["docker_env"] = dict(env or {})
            return {"PATH": f"{env['PATH']}:/docker/bin"}

        def fake_which(command, path=None):
            observed["which"] = {"command": command, "path": path}
            return "/docker/bin/docker" if command == "docker" and "/docker/bin" in str(path) else None

        with patch.dict(os.environ, {"PATH": "/api/process/bin", "MN_HOME": "/api/mn-home"}, clear=True), patch.object(
            blueprints_module,
            "subprocess_environment",
            return_value={"PATH": "/config/bin", "MN_HOME": "/config/mn-home"},
        ), patch.object(
            blueprints_module,
            "sdk_runtime_path_environment",
            side_effect=fake_sdk_runtime_path_environment,
        ), patch.object(
            blueprints_module,
            "docker_cli_path_environment",
            side_effect=fake_docker_cli_path_environment,
        ), patch.object(
            blueprints_module.shutil,
            "which",
            side_effect=fake_which,
        ):
            env = blueprints_module.runtime_path_environment()

        self.assertEqual(observed["sdk_env"]["PATH"], "/api/process/bin")
        self.assertEqual(observed["sdk_env"]["MN_HOME"], "/api/mn-home")
        self.assertEqual(observed["docker_env"]["PATH"], "/api/process/bin")
        self.assertEqual(observed["which"]["path"], "/api/process/bin:/docker/bin")
        self.assertEqual(env["PATH"], "/docker/bin:/api/process/bin")
        self.assertEqual(env["MN_DOCKER_BIN"], "/docker/bin/docker")
        self.assertEqual(env["PYTHONPATH"], "/runtime/python")

    def test_catalog_accepts_wrapped_index_and_normalizes_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            manifest_dir = repo / "worker.two"
            manifest_dir.mkdir()
            (manifest_dir / "manifest.json").write_text(
                json.dumps(
                    {
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                        "metadata": {
                            "init_config_review": {
                                "required": True,
                                "fields": [{"path": "vl_model.model", "label": "VL model"}],
                            }
                        }
                    }
                )
            )
            (repo / "index.json").write_text(
                json.dumps(
                    {
                        "blueprints": [
                            {
                                "blueprintId": "worker.two",
                                "product": {
                                    "name": "Worker Two",
                                    "one_line": "Does useful work.",
                                    "category": "Engineering",
                                    "runtimeFeatures": ["streams"],
                                },
                                "pricing": {
                                    "model": "metered",
                                    "rate": "12.5",
                                    "unit": "run",
                                },
                                "capabilities": "not-a-list",
                            },
                            {"name": "missing id"},
                        ]
                    }
                )
            )

            repo_root, blueprints = load_blueprint_catalog(
                SimpleNamespace(
                    blueprint_source="local",
                    blueprint_repo="",
                    blueprint_local=str(repo),
                    active_blueprint_location=str(repo),
                )
            )

        self.assertEqual(repo_root, repo.resolve())
        self.assertEqual(len(blueprints), 1)
        self.assertEqual(blueprints[0]["id"], "worker.two")
        self.assertEqual(blueprints[0]["name"], "Worker Two")
        self.assertEqual(blueprints[0]["category"], "Engineering")
        self.assertEqual(blueprints[0]["category_slug"], "engineering")
        self.assertEqual(blueprints[0]["pricing"], {"model": "metered", "rate": 12.5, "unit": "run"})
        self.assertEqual(blueprints[0]["rate_label"], "$12.5/run")
        self.assertEqual(blueprints[0]["runtime_features"], ["streams"])
        self.assertEqual(blueprints[0]["capabilities"], [])
        self.assertEqual(blueprints[0]["init_config_review"]["fields"][0]["path"], "vl_model.model")

    @patch("mn_api.blueprints.resolve_runtime_cluster_model_for_api", return_value=None)
    @patch("mn_api.blueprints.resolve_runtime_model_endpoint_for_api", return_value=None)
    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.install_model_entry")
    @patch("mn_api.blueprints.sync_runtime_model_gateways_for_api", return_value={})
    def test_install_blueprint_runtime_models_passes_backend_and_context(
        self,
        mock_sync,
        mock_install,
        mock_installed,
        mock_ledger,
        mock_record,
        _mock_endpoint,
        _mock_cluster,
    ):
        mock_install.return_value = {"compatibility": {"backend": "llama.cpp"}}
        mock_installed.return_value = False
        mock_ledger.return_value = {"version": 2, "models": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "worker_one"},
                "nodes": [],
                "edges": [],
                "runtime": {
                    "models": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "gemma4:e2b",
                            "backend": "llama.cpp",
                            "context_size": 2048,
                        }
                    }
                },
            }))

            summary = install_blueprint_runtime_models(repo.resolve(), {"id": "worker_one", "path": "worker_one"})

        self.assertTrue(summary["ok"])
        mock_install.assert_called_once()
        self.assertEqual(mock_install.call_args.args[0]["id"], "gemma4:e2b")
        self.assertEqual(mock_install.call_args.kwargs["backend"], "llama.cpp")
        self.assertEqual(mock_install.call_args.kwargs["context_size"], 2048)
        self.assertFalse(mock_install.call_args.kwargs["force"])
        mock_record.assert_called_once()

    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_catalog")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.install_model_entry")
    @patch("mn_api.blueprints.sync_runtime_model_gateways_for_api", return_value={})
    def test_install_blueprint_runtime_models_deduplicates_same_docker_model(
        self,
        mock_sync,
        mock_install,
        mock_installed,
        mock_ledger,
        mock_catalog,
        mock_record,
    ):
        mock_install.return_value = {"compatibility": {"backend": "llama.cpp"}}
        mock_installed.return_value = False
        mock_ledger.return_value = {"version": 2, "models": {}}
        mock_catalog.return_value = {
            "gemma4:e2b": {
                "id": "gemma4:e2b",
                "model": "docker.io/ai/gemma4:E2B",
                "provider": "docker_model_runner",
                "aliases": ["default"],
                "backend": "llama.cpp",
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(json.dumps({
                "llm": {
                    "enabled": True,
                    "configs": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "gemma4:e2b",
                        }
                    },
                    "default_config": "primary",
                }
            }))
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "worker_one"},
                "nodes": [],
                "edges": [],
                "runtime": {
                    "models": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "gemma4:e2b",
                            "backend": "llama.cpp",
                        }
                    }
                },
            }))

            summary = install_blueprint_runtime_models(repo.resolve(), {"id": "worker_one", "path": "worker_one"})

        self.assertTrue(summary["ok"])
        self.assertEqual(len(summary["models"]), 2)
        self.assertEqual(summary["models"][0]["status"], "installed")
        self.assertEqual(summary["models"][1]["status"], "installed")
        self.assertEqual(summary["models"][1]["duplicate_of"], "llm.configs.primary")
        mock_install.assert_called_once()
        mock_record.assert_called_once()

    def test_blueprint_requires_context_engine_from_enabled_memory_layer(self):
        manifest = {
            "metadata": {
                "memory_layer": {
                    "enabled": True,
                    "enabled_env": "MN_CONTEXT_MEMORY_ENABLED",
                    "sdk_import_package": "mn_context_engine_sdk",
                }
            }
        }

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(blueprint_requires_context_engine(manifest, None))
            self.assertFalse(
                blueprint_requires_context_engine(
                    manifest,
                    {"memory_layer": {"enabled": False, "enabled_env": "MN_CONTEXT_MEMORY_ENABLED"}},
                )
            )

    @patch("mn_api.blueprints.ensure_context_engine_runtime_direct")
    def test_install_blueprint_runtime_models_ensures_context_engine_when_required(self, mock_ensure):
        mock_ensure.return_value = {"name": "membrane-context-engine", "status": "ready"}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(
                json.dumps(
                    {
                        "memory_layer": {
                            "enabled": True,
                            "enabled_env": "MN_CONTEXT_MEMORY_ENABLED",
                            "sdk_import_package": "mn_context_engine_sdk",
                        }
                    }
                )
            )
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "worker_one"},
                "nodes": [],
                "edges": [],
            }))

            with patch.dict(os.environ, {}, clear=True):
                summary = install_blueprint_runtime_models(repo.resolve(), {"id": "worker_one", "path": "worker_one"})

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["services"][0]["name"], "membrane-context-engine")
        self.assertEqual(summary["services"][0]["status"], "ready")
        mock_ensure.assert_called_once_with(force=False)

    def test_ensure_context_engine_runtime_direct_uses_runtime_environment(self):
        observed = {}
        fake_package = ModuleType("mn_cli")
        fake_package.__path__ = []
        fake_server_cmds = ModuleType("mn_cli.server_cmds")

        def fake_ensure_context_engine_runtime(*, force=False):
            observed["force"] = force
            observed["path"] = os.environ.get("PATH")
            observed["pythonpath"] = os.environ.get("PYTHONPATH")
            return {"status": "already_running"}

        fake_server_cmds.ensure_context_engine_runtime = fake_ensure_context_engine_runtime

        with patch.dict(
            sys.modules,
            {"mn_cli": fake_package, "mn_cli.server_cmds": fake_server_cmds},
        ), patch.dict(
            os.environ,
            {"PATH": "/base/bin"},
            clear=True,
        ), patch.object(
            blueprints_module,
            "subprocess_environment",
            return_value={"PATH": "/config/bin:/base/bin", "MN_API_BASE_URL": "http://api.test"},
        ), patch.object(
            blueprints_module,
            "runtime_path_environment",
            return_value={"PATH": "/runtime/bin:/config/bin:/base/bin", "PYTHONPATH": "/runtime/python"},
        ):
            result = blueprints_module.ensure_context_engine_runtime_direct(force=True)
            self.assertEqual(os.environ.get("PATH"), "/base/bin")
            self.assertNotIn("PYTHONPATH", os.environ)
            self.assertNotIn("MN_API_BASE_URL", os.environ)

        self.assertEqual(result["name"], "membrane-context-engine")
        self.assertEqual(result["status"], "already_running")
        self.assertEqual(
            observed,
            {
                "force": True,
                "path": "/runtime/bin:/config/bin:/base/bin",
                "pythonpath": "/runtime/python",
            },
        )

    @patch("mn_api.blueprints.resolve_runtime_cluster_model_for_api", return_value=None)
    @patch("mn_api.blueprints.resolve_runtime_model_endpoint_for_api", return_value=None)
    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.install_model_entry")
    def test_install_blueprint_runtime_models_failure_does_not_record_owner(
        self,
        mock_install,
        mock_installed,
        mock_ledger,
        mock_record,
        _mock_endpoint,
        _mock_cluster,
    ):
        mock_install.side_effect = RuntimeError("pull failed")
        mock_installed.return_value = False
        mock_ledger.return_value = {"version": 2, "models": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "worker_one"},
                "nodes": [],
                "edges": [],
                "runtime": {
                    "models": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "gemma4:e2b",
                        }
                    }
                },
            }))

            summary = install_blueprint_runtime_models(repo.resolve(), {"id": "worker_one", "path": "worker_one"})

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["models"][0]["status"], "failed")
        self.assertEqual(summary["models"][0]["error"], "pull failed")
        self.assertEqual(summary["errors"], ["pull failed"])
        mock_install.assert_called_once()
        mock_record.assert_not_called()

    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_catalog")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.install_model_entry")
    def test_install_blueprint_runtime_models_cluster_provided_skips_local_install(
        self,
        mock_install,
        mock_installed,
        mock_ledger,
        mock_catalog,
        mock_record,
    ):
        mock_catalog.return_value = {
            "video-vlm:default": {
                "id": "video-vlm:default",
                "model": "huggingface.co/acme/video-vlm",
                "provider": "docker_model_runner",
                "backend": "llama.cpp",
            }
        }
        mock_ledger.return_value = {"version": 2, "models": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "worker_one"},
                "nodes": [],
                "edges": [],
                "runtime": {
                    "models": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "video-vlm:default",
                            "backend": "llama.cpp",
                            "install_mode": "cluster_provided",
                        }
                    }
                },
            }))

            summary = install_blueprint_runtime_models(repo.resolve(), {"id": "worker_one", "path": "worker_one"})

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["models"][0]["status"], "cluster_provided")
        self.assertEqual(summary["models"][0]["model"], "huggingface.co/acme/video-vlm")
        mock_installed.assert_not_called()
        mock_install.assert_not_called()
        mock_record.assert_not_called()

    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_catalog")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.install_model_entry")
    @patch("mn_api.blueprints.sync_runtime_model_gateways_for_api", return_value={})
    def test_install_blueprint_runtime_models_uses_registered_nemotron_remote(
        self,
        mock_sync,
        mock_install,
        mock_installed,
        mock_ledger,
        mock_catalog,
        mock_record,
    ):
        mock_catalog.return_value = {
            "nemotron-3.5-lightning:latest": {
                "id": "nemotron-3.5-lightning:latest",
                "model": "nemotron-3.5-lightning:latest",
                "api_model": "nemotron-3.5-lightning:latest",
                "provider": "docker_model_runner",
                "backend": "llama.cpp",
                "aliases": ["nemotron-3.5-lightning"],
            }
        }
        mock_ledger.return_value = {"version": 2, "models": {}}
        observed: dict[str, dict] = {}

        def gateway_sync(summary):
            observed["upstream"] = json.loads(json.dumps(summary["endpoints"]))
            return blueprints_module.gateway_endpoint_map(summary["endpoints"])

        mock_sync.side_effect = gateway_sync
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            remotes_path = repo / "model-remotes.json"
            remotes_path.write_text(json.dumps({
                "version": 2,
                "remotes": {
                    "spark": {
                        "name": "spark",
                        "provider": "docker_model_runner",
                        "model": "nemotron-3.5-lightning:latest",
                        "api_model": "nemotron-3.5-lightning:latest",
                        "base_url": "http://192.168.4.173:12434/v1",
                        "api_key": "not-needed",
                        "node": "spark",
                    }
                },
            }))
            bundle = repo / "vc_assistant"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "vc_assistant"},
                "nodes": [],
                "edges": [],
                "runtime": {
                    "models": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "nemotron-3.5-lightning:latest",
                            "api_base": "auto",
                            "backend": "llama.cpp",
                        }
                    }
                },
            }))

            with patch.dict(os.environ, {"MN_MODEL_REMOTES_PATH": str(remotes_path)}, clear=False):
                summary = install_blueprint_runtime_models(repo.resolve(), {"id": "vc_assistant", "path": "vc_assistant"})

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["models"][0]["status"], "model_remote")
        self.assertEqual(summary["models"][0]["endpoint"]["api_base"], "http://192.168.4.173:12434/v1")
        mock_sync.assert_called_once()
        self.assertEqual(
            observed["upstream"]["nemotron-3.5-lightning:latest"]["api_base"],
            "http://192.168.4.173:12434/v1",
        )
        self.assertEqual(
            json.loads(summary["env"]["MN_MODEL_ENDPOINTS_JSON"])["nemotron-3.5-lightning:latest"]["api_base"],
            "http://mn-litellm-proxy:4000/v1",
        )
        mock_installed.assert_not_called()
        mock_install.assert_not_called()
        mock_record.assert_not_called()

    @patch("mn_api.blueprints.sync_litellm_gateway", return_value={"status": "running"})
    @patch("mn_api.blueprints.load_model_catalog")
    @patch("mn_api.blueprints.load_model_ownership")
    def test_install_blueprint_runtime_models_prepares_gemma_fallback_on_single_node(
        self, mock_ledger, mock_catalog, mock_sync
    ):
        mock_catalog.return_value = {
            "gemma4:e2b": {
                "id": "gemma4:e2b",
                "model": "docker.io/ai/gemma4:E2B",
                "api_model": "docker.io/ai/gemma4:E2B",
                "provider": "docker_model_runner",
                "aliases": ["default", "small", "gemma4"],
                "backend": "llama.cpp",
                "requirements": {"min_vram_gb": 8, "min_unified_memory_gb": 16},
            },
            "nemotron-3.5-lightning:latest": {
                "id": "nemotron-3.5-lightning:latest",
                "model": "nemotron-3.5-lightning:latest",
                "tag_name": "nemotron-3.5-lightning:latest",
                "api_model": "nemotron-3.5-lightning:latest",
                "provider": "docker_model_runner",
                "aliases": ["medium", "nemotron-3.5-lightning:latest"],
                "backend": "llama.cpp",
                "fallback_model": "gemma4:e2b",
                "requirements": {"min_vram_gb": 48, "min_unified_memory_gb": 48},
            },
        }
        mock_ledger.return_value = {"version": 2, "models": {}}
        resources = {
            "nodes": [
                {
                    "name": "local",
                    "self": True,
                    "status": "healthy",
                    "scheduling_eligible": True,
                    "devices": [{"kind": "gpu", "memory_total_mb": 16384}],
                }
            ]
        }
        systems = {
            "nodes": [
                {
                    "name": "local",
                    "self": True,
                    "status": "healthy",
                    "scheduling_eligible": True,
                    "grpc_host": "10.0.0.10",
                }
            ]
        }
        prepared = {
            "status": "installed",
            "docker_model": "docker.io/ai/gemma4:E2B",
            "endpoint": {
                "model": "docker.io/ai/gemma4:E2B",
                "runtime_model": "docker.io/ai/gemma4:E2B",
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("mn_api.state.client") as mock_client,
            patch("mn_api.blueprints.resolve_runtime_model_endpoint_for_api", return_value=None),
        ):
            repo = Path(tmpdir)
            bundle = repo / "vc_assistant"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(json.dumps({
                "llm": {
                    "enabled": True,
                    "model": "default",
                    "provider": "docker_model_runner",
                }
            }))
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "vc_assistant"},
                "nodes": [],
                "edges": [],
                "runtime": {"models": {"primary": {"provider": "docker_model_runner", "model": "default"}}},
            }))
            mock_client.resolve_service.return_value = json.dumps({"services": []})
            mock_client.get_resource.return_value = json.dumps(resources)
            mock_client.get_system_summary.return_value = json.dumps(systems)
            mock_client.prepare_runtime_model.return_value = json.dumps(prepared)
            summary = install_blueprint_runtime_models(
                repo.resolve(), {"id": "vc_assistant", "path": "vc_assistant"}
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["models"][0]["status"], "fallback_model")
        self.assertEqual(summary["models"][0]["fallback"]["id"], "gemma4:e2b")
        self.assertEqual(
            json.loads(summary["env"]["MN_BLUEPRINT_CONFIG_JSON"])["llm"]["model"],
            "gemma4:e2b",
        )
        self.assertEqual(
            json.loads(summary["env"]["MN_BLUEPRINT_CONFIG_JSON"])["llm"]["api_base"],
            "http://mn-litellm-proxy:4000/v1",
        )
        self.assertEqual(summary["env"]["MN_LLM_MODEL"], "default")
        self.assertEqual(
            json.loads(summary["env"]["MN_MODEL_ENDPOINTS_JSON"])["small"]["api_base"],
            "http://mn-litellm-proxy:4000/v1",
        )
        mock_client.prepare_runtime_model.assert_called_once()
        mock_sync.assert_called_once()

    @patch("mn_api.blueprints.sync_litellm_gateway", return_value={"status": "running"})
    @patch("mn_api.blueprints.load_model_catalog")
    @patch("mn_api.blueprints.load_model_ownership")
    def test_install_blueprint_runtime_models_prepares_normal_model_on_remote_node_and_fans_out_gateway(
        self, mock_ledger, mock_catalog, mock_sync
    ):
        mock_catalog.return_value = {
            "nemotron-3.5-lightning:latest": {
                "id": "nemotron-3.5-lightning:latest",
                "model": "nemotron-3.5-lightning:latest",
                "tag_name": "nemotron-3.5-lightning:latest",
                "api_model": "nemotron-3.5-lightning:latest",
                "provider": "docker_model_runner",
                "aliases": ["medium", "nemotron-3.5-lightning:latest"],
                "route_aliases": ["nemotron-3.5-lightning:latest"],
                "backend": "llama.cpp",
                "requirements": {"min_vram_gb": 48, "min_unified_memory_gb": 48},
            }
        }
        mock_ledger.return_value = {"version": 2, "models": {}}
        resources = {
            "nodes": [
                {
                    "name": "local", "self": True, "status": "healthy",
                    "scheduling_eligible": True,
                    "devices": [{"kind": "gpu", "memory_total_mb": 16384}],
                },
                {
                    "name": "remote", "status": "healthy",
                    "scheduling_eligible": True,
                    "devices": [{"kind": "gpu", "memory_total_mb": 65536}],
                },
            ]
        }
        systems = {
            "nodes": [
                {"name": "local", "self": True, "status": "healthy", "scheduling_eligible": True, "grpc_host": "10.0.0.10"},
                {
                    "name": "remote",
                    "status": "healthy",
                    "scheduling_eligible": True,
                    "grpc_host": "10.0.0.20",
                    "native_sdk_grpc": {"host": "10.0.0.20", "port": 55052, "target": "10.0.0.20:55052"},
                },
            ]
        }
        remote_runtime = unittest.mock.Mock()
        remote_runtime.prepare_runtime_model.return_value = json.dumps({
            "status": "installed",
            "docker_model": "nemotron-3.5-lightning:latest",
            "endpoint": {"model": "nemotron-3.5-lightning:latest", "runtime_model": "nemotron-3.5-lightning:latest"},
        })
        remote_runtime.sync_litellm_gateway.return_value = json.dumps({"status": "running"})
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("mn_api.state.client") as mock_client,
            patch("mn_api.blueprints.Client", return_value=remote_runtime) as client_class,
            patch("mn_api.state.refresh_config_from_env", return_value=SimpleNamespace(grpc_auth_token="auth", grpc_admin_token="admin")),
            patch("mn_api.blueprints.resolve_runtime_model_endpoint_for_api", return_value=None),
        ):
            repo = Path(tmpdir)
            bundle = repo / "vc_assistant"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(json.dumps({"llm": {"enabled": True, "model": "medium", "provider": "docker_model_runner"}}))
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "metadata": {"blueprint_id": "vc_assistant"}, "nodes": [], "edges": [],
                "runtime": {"models": {"primary": {"provider": "docker_model_runner", "model": "medium"}}},
            }))
            mock_client.resolve_service.return_value = json.dumps({"services": []})
            mock_client.get_resource.return_value = json.dumps(resources)
            mock_client.get_system_summary.return_value = json.dumps(systems)
            summary = install_blueprint_runtime_models(repo.resolve(), {"id": "vc_assistant", "path": "vc_assistant"})

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["models"][0]["cluster"]["node"], "remote")
        self.assertEqual(summary["models"][0]["endpoint"]["api_base"], "http://10.0.0.20:4000/v1")
        mock_client.prepare_runtime_model.assert_not_called()
        remote_runtime.prepare_runtime_model.assert_called_once()
        self.assertEqual(client_class.call_count, 2)
        remote_runtime.sync_litellm_gateway.assert_called_once()
        fanout_payload = remote_runtime.sync_litellm_gateway.call_args.args[0]
        self.assertEqual(fanout_payload["runtime_endpoints"]["medium"]["api_base"], "http://10.0.0.20:4000/v1")
        self.assertEqual(
            json.loads(summary["env"]["MN_MODEL_ENDPOINTS_JSON"])["medium"]["api_base"],
            "http://mn-litellm-proxy:4000/v1",
        )
        mock_sync.assert_called_once()

    @patch("mn_api.blueprints.sync_runtime_model_gateways_for_api", side_effect=RuntimeError("proxy unavailable"))
    @patch("mn_api.blueprints.blueprint_model_dependency_summary")
    @patch("mn_api.blueprints.load_model_catalog", return_value={})
    def test_install_blueprint_runtime_models_blocks_launch_when_gateway_sync_fails(
        self, mock_catalog, mock_summary, mock_sync
    ):
        mock_summary.return_value = {
            "models": [
                {
                    "id": "gemma4:e2b",
                    "model": "docker.io/ai/gemma4:E2B",
                    "provider": "docker_model_runner",
                    "status": "installed",
                }
            ],
            "endpoints": {"small": {"model": "docker.io/ai/gemma4:E2B"}},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},"nodes": [], "edges": []}))
            summary = install_blueprint_runtime_models(
                repo.resolve(), {"id": "worker_one", "path": "worker_one"}
            )

        self.assertFalse(summary["ok"])
        self.assertIn("LiteLLM gateway synchronization failed: proxy unavailable", summary["errors"])
        mock_sync.assert_called_once()

    def test_catalog_loads_category_facets_and_filters_by_slug_or_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "category.json").write_text(
                json.dumps(
                    {
                        "categories": [
                            {"name": "Business", "slug": "business"},
                            {"name": "Finance", "slug": "finance"},
                        ]
                    }
                )
            )
            (repo / "index.json").write_text(
                json.dumps(
                    [
                        {"id": "business_worker", "name": "Business Worker", "category": "Business"},
                        {"id": "finance_worker", "name": "Finance Worker", "category": "Finance"},
                        {"id": "another_finance_worker", "name": "Another Finance Worker", "category": "finance"},
                    ]
                )
            )

            repo_root, blueprints = load_blueprint_catalog(
                SimpleNamespace(
                    blueprint_source="local",
                    blueprint_repo="",
                    blueprint_local=str(repo),
                    active_blueprint_location=str(repo),
                )
            )
            categories = load_blueprint_categories(repo_root, blueprints)

        self.assertEqual(
            categories,
            [
                {"name": "Business", "slug": "business", "count": 1},
                {"name": "Finance", "slug": "finance", "count": 2},
            ],
        )
        self.assertEqual(
            [blueprint["id"] for blueprint in filter_blueprints_by_category(blueprints, "finance")],
            ["finance_worker", "another_finance_worker"],
        )
        self.assertEqual(
            [blueprint["id"] for blueprint in filter_blueprints_by_category(blueprints, "Business,finance")],
            ["business_worker", "finance_worker", "another_finance_worker"],
        )

    def test_catalog_rejects_non_list_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "index.json").write_text(json.dumps({"blueprints": {"id": "not-a-list"}}))

            with self.assertRaises(HTTPException) as raised:
                load_blueprint_catalog(
                    SimpleNamespace(
                        blueprint_source="local",
                        blueprint_repo="",
                        blueprint_local=str(repo),
                        active_blueprint_location=str(repo),
                    )
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "blueprint repo index.json must be a list")

    def test_blueprint_bundle_root_rejects_paths_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()

            with self.assertRaises(HTTPException) as raised:
                blueprint_bundle_root(repo, {"id": "bad", "path": "../outside"})

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "blueprint path escapes repository")

    def test_load_blueprint_bundle_sets_run_metadata_and_reads_nested_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            payloads = bundle / "payloads" / "nested"
            payloads.mkdir(parents=True)
            config_dir = bundle / "config"
            config_dir.mkdir()
            (payloads / "input.txt").write_bytes(b"hello")
            (config_dir / "default.json").write_text(
                json.dumps({
                    "identity": {"blueprint_id": "worker_one"},
                    "vl_model": {"model": "default"},
                    "manifest_config_bindings": [
                        {
                            "config_path": "vl_model.model",
                            "manifest_path": "agents.nodes.worker.config.environment.CUSTOM_MODEL",
                        }
                    ],
                })
            )
            (config_dir / "overwrite.json").write_text(json.dumps({"vl_model": {"model": "overwrite"}}))
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {"environment": {"MN_LLM_MODEL": "ollama/test"}},
                            }
                        ]},
                        "runtime": {},
                        "metadata": "replace-me",
                    }
                )
            )

            manifest_json, payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one", "revision": "rev-7"},
                "run-7",
                config_overrides={"vl_model": {"base_url": "http://local"}},
            )

        manifest = json.loads(manifest_json)
        self.assertEqual(manifest["run_id"], "run-7")
        self.assertEqual(manifest["metadata"]["blueprint_id"], "worker_one")
        self.assertEqual(manifest["metadata"]["blueprint_revision"], "rev-7")
        env = manifest["flow"]["nodes"][0]["config"]["environment"]
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(injected_config["vl_model"], {"model": "overwrite", "base_url": "http://local"})
        self.assertEqual(env["VL_MODEL_NAME"], "overwrite")
        self.assertEqual(env["OLLAMA_MODEL"], "overwrite")
        self.assertEqual(env["VL_MODEL_BASE_URL"], "http://local")
        self.assertEqual(env["CUSTOM_MODEL"], "overwrite")
        self.assertEqual(env["MN_LLM_MODEL"], "ollama/test")
        self.assertEqual(payload_bytes, {"nested/input.txt": b"hello"})

    def test_load_blueprint_bundle_uses_shared_sdk_manifest_preparation(self):
        def fake_prepare_job_submission(manifest, payloads, **_kwargs):
            return SimpleNamespace(manifest_json=json.dumps(manifest), payloads=payloads)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                        "kind": "Workflow",
                        "workflow": {
                            "workflow_id": "worker_one_v2",
                            "steps": [{"id": "review", "run": "review"}],
                        },
                        "agents": {"nodes": [{"node_id": "review", "config": {}}]},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                blueprints_module,
                "prepare_manifest_submission",
                wraps=blueprints_module.prepare_manifest_submission,
            ) as shared_prepare, patch.object(
                blueprints_module,
                "prepare_job_submission",
                side_effect=fake_prepare_job_submission,
            ):
                manifest_json, _payloads = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "worker_one", "path": "worker_one"},
                    "worker_one-346dab41d3",
                )

        manifest = json.loads(manifest_json)
        shared_prepare.assert_called_once()
        self.assertEqual(manifest["flow"]["steps"], manifest["workflow"]["steps"])
        self.assertEqual([node["node_id"] for node in manifest["flow"]["nodes"]], ["review"])
        self.assertNotIn("nodes", manifest)

    def test_load_blueprint_bundle_prepares_submission_with_runtime_process_environment(self):
        observed = {}

        def fake_prepare_job_submission(
            manifest,
            payloads,
            *,
            bundle_dir,
            run_id,
            cluster_client=None,
            **_kwargs,
        ):
            observed["path"] = os.environ.get("PATH")
            observed["docker_bin"] = os.environ.get("MN_DOCKER_BIN")
            observed["cluster_client"] = cluster_client
            return SimpleNamespace(manifest_json=json.dumps(manifest), payloads=payloads)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {"runner_module": "MirrorNeuron.Runner.DockerWorker"},
                            }
                        ], "edges": []},
                        "runtime": {},
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"PATH": "/api/bin"}, clear=True), patch.object(
                blueprints_module,
                "runtime_process_environment",
                return_value={"PATH": "/docker/bin:/api/bin", "MN_DOCKER_BIN": "/docker/bin/docker"},
            ), patch.object(
                blueprints_module,
                "prepare_job_submission",
                side_effect=fake_prepare_job_submission,
            ), patch.object(
                state.client,
                "get_resource",
                return_value=_single_node_resource_report(),
            ), patch.object(
                state.client,
                "get_system_summary",
                return_value=_single_node_resource_report(),
            ):
                _manifest_json, _payload_bytes = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "worker_one", "path": "worker_one"},
                    "run-env",
                    progress_callback=lambda message, detail, expectation: observed.update(
                        {"message": message, "detail": detail, "expectation": expectation}
                    ),
                )
                self.assertEqual(os.environ.get("PATH"), "/api/bin")
                self.assertNotIn("MN_DOCKER_BIN", os.environ)

        self.assertEqual(observed["path"], "/docker/bin:/api/bin")
        self.assertEqual(observed["docker_bin"], "/docker/bin/docker")
        self.assertIs(observed["cluster_client"], state.client)
        self.assertEqual(observed["message"], "Preparing DockerWorker runtime.")
        self.assertIn("DockerWorker image", observed["detail"])
        self.assertIn("several minutes", observed["expectation"])

    def test_load_blueprint_bundle_preserves_requested_single_node_owner(self):
        owner = "mirror_neuron@10.0.4.26"
        local = "mirror_neuron@10.0.4.23"
        node = lambda name, self: {
            "name": name,
            "status": "healthy",
            "scheduling_eligible": True,
            "self": self,
            "connection_mode": "federated" if not self else "local",
            "coordination_store": {
                "identity": f"{name}-store",
                "writable_primary": True,
                "healthy": True,
            },
            "hardware": {
                "cpu": {"logical_processors": 8},
                "memory": {"total_mb": 16384},
                "native_sdk_grpc": {
                    "enabled": True,
                    "target": f"{name}:55052",
                    "capabilities": ["docker_worker_prepare_v1"],
                },
            },
        }
        report = json.dumps({"nodes": [node(local, True), node(owner, False)]})

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "runtime": {},
                        "agents": {
                            "nodes": [
                                {
                                    "node_id": "worker",
                                    "config": {
                                        "runner_module": "MirrorNeuron.Runner.DockerWorker"
                                    },
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                blueprints_module,
                "prepare_job_submission",
                side_effect=lambda manifest, payloads, **_kwargs: SimpleNamespace(
                    manifest_json=json.dumps(manifest), payloads=payloads
                ),
            ), patch.object(state.client, "get_resource", return_value=report), patch.object(
                state.client, "get_system_summary", return_value=report
            ):
                manifest_json, _payloads = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "worker_one", "path": "worker_one"},
                    "run-owner",
                    env_overrides={"MN_SELECTED_RUNTIME_NODE": owner},
                )

        manifest = json.loads(manifest_json)
        placement = manifest["metadata"]["mn_workflow_placement"]
        self.assertEqual(placement["selected_node"], owner)
        for workflow_node in manifest["flow"]["nodes"]:
            self.assertEqual(
                workflow_node["policies"]["scheduler"]["preferred_node"], owner
            )

    def test_load_blueprint_bundle_prepares_hostlocal_python_environment(self):
        observed = {}

        def fake_prepare_runtime_model(_client, payload, **_kwargs):
            observed.update(payload)
            return {
                "status": "ready",
                "runtime_path": "/runtime/shared/blueprint-python-envs/env-1",
                "host_path": "/host/shared/blueprint-python-envs/env-1",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "host_worker"
            requirements = bundle / "payloads" / "worker" / "requirements.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_text("requests==2.32.0\n", encoding="utf-8")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {
                                    "runner_module": "MirrorNeuron.Runner.HostLocal",
                                    "python_environment": {
                                        "packages": ["fastapi>=0.115"],
                                        "requirements": "worker/requirements.txt",
                                    },
                                },
                            }
                        ]},
                        "runtime": {"placement": {"mode": "distributed"}},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                blueprints_module,
                "call_prepare_runtime_model",
                side_effect=fake_prepare_runtime_model,
            ), patch.object(
                blueprints_module,
                "hostlocal_runtime_client",
                return_value=object(),
            ), patch.object(
                state.client,
                "get_resource",
                return_value=json.dumps(
                    {
                        "nodes": [
                            {
                                "name": "worker-a",
                                "status": "healthy",
                                "scheduling_eligible": True,
                                "coordination_store": {
                                    "identity": "test-store",
                                    "writable_primary": True,
                                    "healthy": True,
                                },
                            }
                        ]
                    }
                ),
            ), patch.object(
                state.client,
                "get_system_summary",
                return_value=json.dumps(
                    {
                        "nodes": [
                            {
                                "name": "worker-a",
                                "status": "healthy",
                                "scheduling_eligible": True,
                                "self": True,
                                "coordination_store": {
                                    "identity": "test-store",
                                    "writable_primary": True,
                                    "healthy": True,
                                },
                            }
                        ]
                    }
                ),
            ):
                manifest_json, _payloads = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "host_worker", "path": "host_worker"},
                    "host-run",
                    env_overrides={"MN_SELECTED_RUNTIME_NODE": "worker-a"},
                )

        manifest = json.loads(manifest_json)
        python_environment = manifest["flow"]["nodes"][0]["config"]["python_environment"]
        self.assertEqual(
            python_environment["path"],
            "/runtime/shared/blueprint-python-envs/env-1",
        )
        self.assertEqual(observed["node"], "worker-a")
        self.assertEqual(observed["blueprint_id"], "host_worker")
        self.assertEqual(observed["node_id"], "worker")
        self.assertEqual(observed["packages"], ["fastapi>=0.115"])
        self.assertEqual(observed["requirements_content"], "requests==2.32.0\n")
        self.assertNotIn("local_source_versions", observed)
        self.assertTrue(observed["ensure_hostlocal_python_environment"])

    def test_hostlocal_local_source_versions_uses_declared_local_dependency_version(self):
        source = Path("/tmp/local-skill")
        manifest = {
            "metadata": {
                "mn_local_skill_dependencies": {
                    "sources": [
                        {
                            "source": str(source),
                            "version": "1.2.31",
                        }
                    ]
                }
            }
        }

        versions = blueprints_module.hostlocal_local_source_versions(
            manifest,
            [str(source), "requests>=2.32"],
        )

        self.assertEqual(versions, {str(source): "1.2.31"})

    def test_load_blueprint_bundle_localizes_declared_agent_dependencies_like_cli(self):
        observed = {}

        def fake_prepare_job_submission(manifest, payloads, **_kwargs):
            observed["manifest"] = manifest
            observed["payloads"] = payloads
            return SimpleNamespace(manifest_json=json.dumps(manifest), payloads=payloads)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            agents_root = repo / "mn-agents"
            agent_source = agents_root / "prototype_stateful_step_agent"
            package_dir = agent_source / "mn_prototype_stateful_step_agent"
            package_dir.mkdir(parents=True)
            (agent_source / "pyproject.toml").write_text(
                "[project]\nname = 'mn-prototype-stateful-step-agent'\nversion = '9.9.9'\n",
                encoding="utf-8",
            )
            (package_dir / "__init__.py").write_text(
                "class AgentHandlerOutput:\n    pass\n",
                encoding="utf-8",
            )

            bundle = repo / "vc_assistant"
            context = bundle / "payloads" / "docker_worker"
            context.mkdir(parents=True)
            (context / "Dockerfile").write_text(
                "FROM python:3.11-slim\n"
                "COPY requirements.txt /tmp/mn-skill-runtime/requirements.txt\n"
                "COPY local-requirements.txt /tmp/mn-skill-runtime/local-requirements.txt\n"
                "# mirrorneuron: skill-dependencies\n"
                "# mirrorneuron: skill-dependencies-end\n"
                "RUN python3 -m pip install -r /tmp/mn-skill-runtime/requirements.txt\n"
                "RUN python3 -m pip install -r /tmp/mn-skill-runtime/local-requirements.txt\n",
                encoding="utf-8",
            )
            (context / "requirements.txt").write_text("", encoding="utf-8")
            (context / "local-requirements.txt").write_text("", encoding="utf-8")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {
                                    "runner_module": "MirrorNeuron.Runner.DockerWorker",
                                    "docker_worker_image": "docker_worker",
                                    "image": "mirror-neuron/vc-assistant:test",
                                },
                            }
                        ]},
                        "runtime": {},
                        "agent_dependencies": [
                            {
                                "type": "pip",
                                "source": "gar",
                                "name": "mn-prototype-stateful-step-agent",
                                "version": "1.2.24",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "MN_USE_LOCAL_SKILLS": "1",
                    "MN_AGENTS_ROOT": str(agents_root),
                },
            ), patch.object(
                blueprints_module,
                "prepare_job_submission",
                side_effect=fake_prepare_job_submission,
            ), patch.object(
                state.client,
                "get_resource",
                return_value=_single_node_resource_report(),
            ), patch.object(
                state.client,
                "get_system_summary",
                return_value=_single_node_resource_report(),
            ):
                load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "vc_assistant", "path": "vc_assistant"},
                    "agent-dependency-run",
                )

        manifest = observed["manifest"]
        self.assertEqual(manifest["agent_dependencies"], [])
        local = manifest["metadata"]["mn_local_skill_dependencies"]
        self.assertIn("mn-prototype-stateful-step-agent", local["packages"])
        payloads = observed["payloads"]
        staged_prefix = (
            "docker_worker/__mn_skill_dependencies/local/"
            "prototype_stateful_step_agent"
        )
        self.assertIn(f"{staged_prefix}/pyproject.toml", payloads)
        self.assertIn(
            "/tmp/mn-skill-runtime/local/prototype_stateful_step_agent",
            payloads["docker_worker/local-requirements.txt"].decode("utf-8"),
        )

    def test_load_blueprint_bundle_injects_docker_model_runner_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(
                json.dumps(
                    {
                        "llm": {
                            "enabled": True,
                            "default_config": "primary",
                            "configs": {
                                "primary": {
                                    "provider": "docker_model_runner",
                                    "runtime_model": "gemma4:e2b",
                                    "api_base": "auto",
                                    "backend": "llama.cpp",
                                    "context_size": 4096,
                                }
                            },
                        }
                    }
                )
            )
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {
                                    "runner_module": "MirrorNeuron.Runner.HostLocal",
                                    "environment": {},
                                },
                            }
                        ]},
                        "runtime": {},
                    }
                )
            )

            with patch.object(
                state.client,
                "get_resource",
                return_value=_single_node_resource_report(),
            ), patch.object(
                state.client,
                "get_system_summary",
                return_value=_single_node_resource_report(),
            ):
                manifest_json, _payload_bytes = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "worker_one", "path": "worker_one"},
                    "run-7",
                )

        manifest = json.loads(manifest_json)
        env = manifest["flow"]["nodes"][0]["config"]["environment"]
        self.assertEqual(env["MN_LLM_PROVIDER"], "litellm")
        self.assertEqual(env["MN_LLM_MODEL"], "docker.io/ai/gemma4:E2B")
        self.assertEqual(env["MN_LLM_API_BASE"], "http://127.0.0.1:4000/v1")

    def test_load_blueprint_bundle_stages_configured_local_input_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            host_docs = repo / "host_tax_docs"
            host_docs.mkdir()
            (host_docs / "w2.txt").write_text("box 1 wages 100\n")
            (host_docs / "ignore.csv").write_text("skip\n")
            bundle = repo / "tax_worker"
            (bundle / "payloads" / "tax_workflow").mkdir(parents=True)
            config_dir = bundle / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(
                json.dumps(
                    {
                        "tax_documents": {"folder_path": ""},
                        "inputs": {"payload": {"document_folder": ""}},
                        "local_inputs": {
                            "folders": [
                                {
                                    "config_path": "tax_documents.folder_path",
                                    "payload_path": "tax_workflow/mn_local_inputs/tax_documents",
                                    "runtime_path": "mn_local_inputs/tax_documents",
                                    "allowed_extensions": [".txt"],
                                    "linked_config_paths": ["inputs.payload.document_folder"],
                                }
                            ]
                        },
                    }
                )
            )
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "document_intake_agent",
                                "config": {"environment": {}},
                            }
                        ]},
                        "runtime": {},
                        "metadata": {},
                    }
                )
            )

            with patch.dict(
                os.environ,
                {
                    "MN_SHARED_STORAGE_ROOT": str(repo / "shared"),
                    "MN_RUNTIME_SHARED_STORAGE_ROOT": "/runtime/shared",
                },
            ):
                manifest_json, payload_bytes = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "tax_worker", "path": "tax_worker"},
                    "tax-run-1",
                    config_overrides={"tax_documents": {"folder_path": str(host_docs)}},
                )

        manifest = json.loads(manifest_json)
        env = manifest["flow"]["nodes"][0]["config"]["environment"]
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        expected_input = "/runtime/shared/submissions"
        self.assertTrue(injected_config["tax_documents"]["folder_path"].startswith(expected_input))
        self.assertTrue(injected_config["tax_documents"]["folder_path"].endswith("/inputs/tax_workflow/mn_local_inputs/tax_documents"))
        self.assertEqual(
            injected_config["inputs"]["payload"]["document_folder"],
            injected_config["tax_documents"]["folder_path"],
        )
        self.assertNotIn("tax_workflow/mn_local_inputs/tax_documents/w2.txt", payload_bytes)
        self.assertNotIn("tax_workflow/mn_local_inputs/tax_documents/ignore.csv", payload_bytes)
        self.assertEqual(manifest["metadata"]["mn_local_inputs"]["folders"][0]["file_count"], 1)

    def test_dirty_hosted_git_cache_is_reset_before_pull(self):
        repo_url = "https://example.test/MirrorNeuronLab/otterdesk-blueprints.git"
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = Path(tmpdir) / "cache"
            with patch.dict("os.environ", {"MN_BLUEPRINT_REPO_CACHE": str(cache_root)}):
                target = cached_git_repo_path(repo_url)
                (target / ".git").mkdir(parents=True)
                (target / "index.json").write_text("[]")
                calls = []

                def fake_run(command, **_kwargs):
                    calls.append(command)
                    if command[-2:] == ["status", "--porcelain"]:
                        return SimpleNamespace(stdout=" M index.json\n?? personal_income_tax_expert/\n", stderr="")
                    return SimpleNamespace(stdout="", stderr="")

                with patch("mn_api.blueprints.subprocess.run", side_effect=fake_run):
                    result = ensure_git_blueprint_repo(repo_url)

        self.assertEqual(result, target)
        self.assertIn(["git", "-C", str(target), "reset", "--hard", "HEAD"], calls)
        self.assertIn(["git", "-C", str(target), "clean", "-fdx"], calls)
        self.assertIn(["git", "-C", str(target), "pull", "--ff-only"], calls)

    def test_load_blueprint_bundle_prepares_openshell_custom_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            sandbox = bundle / "payloads" / "worker" / "openshell_sandbox"
            sandbox.mkdir(parents=True)
            (sandbox / "Dockerfile").write_text("FROM alpine\n")
            (bundle / "manifest.json").write_text(
                json.dumps({
                    "apiVersion": "mn.workflow/v1",
                    "kind": "Workflow",
                    "id": "test-workflow",
                    "contract": {},
                    "agents": {"nodes": [
                        {
                            "node_id": "worker",
                            "config": {
                                "runner_module": "MirrorNeuron.Sandbox.OpenShell",
                                "custom_openshell_image": "worker/openshell_sandbox",
                            },
                        }
                    ]},
                    "runtime": {},
                    "metadata": {},
                })
            )
            calls = []

            def fake_run(command, **_kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("mn_api.blueprints.openshell_gateway_uses_local_docker", return_value=True):
                with patch("mn_api.blueprints.subprocess.run", side_effect=fake_run):
                    manifest_json, _payloads = load_blueprint_bundle(
                        repo.resolve(),
                        {"id": "worker_one", "path": "worker_one"},
                        "run-openshell",
                    )

        manifest = json.loads(manifest_json)
        config = manifest["flow"]["nodes"][0]["config"]
        self.assertTrue(config["from"].startswith("openshell/sandbox-from:"))
        self.assertEqual(calls[0][:3], ["docker", "build", "-t"])

    def test_load_blueprint_bundle_ignores_misnamed_overwrite_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(json.dumps({"vl_model": {"model": "default"}}))
            (config_dir / "overwrites.json").write_text(json.dumps({"vl_model": {"model": "wrong-name"}}))
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {"environment": {}},
                            }
                        ]},
                        "runtime": {},
                    }
                )
            )

            manifest_json, _payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one"},
                "run-1",
            )

        env = json.loads(manifest_json)["flow"]["nodes"][0]["config"]["environment"]
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(injected_config["vl_model"]["model"], "default")
        self.assertNotIn("VL_MODEL_NAME", env)

    def test_load_blueprint_bundle_rejects_invalid_overwrite_data_format(self):
        for payload, expected_detail in (
            ("[]", "overwrite.json must contain a JSON object"),
            ("{bad json", "overwrite.json is malformed"),
        ):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmpdir:
                    repo = Path(tmpdir)
                    bundle = repo / "worker_one"
                    config_dir = bundle / "config"
                    config_dir.mkdir(parents=True)
                    (config_dir / "default.json").write_text(json.dumps({"vl_model": {"model": "default"}}))
                    (config_dir / "overwrite.json").write_text(payload)
                    (bundle / "manifest.json").write_text(json.dumps({
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},"nodes": []}))

                    with self.assertRaises(HTTPException) as raised:
                        load_blueprint_bundle(
                            repo.resolve(),
                            {"id": "worker_one", "path": "worker_one"},
                            "run-1",
                        )

                self.assertEqual(raised.exception.status_code, 500)
                self.assertEqual(raised.exception.detail, expected_detail)

    def test_manifest_config_bindings_ignore_wrong_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            config_dir = bundle / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "default.json").write_text(
                json.dumps(
                    {
                        "vl_model": {"model": "overwrite"},
                        "manifest_config_bindings": [
                            {
                                "config_path": "vl_model.wrong_name",
                                "manifest_path": "agents.nodes.worker.config.environment.CUSTOM_MODEL",
                            },
                            {
                                "config_path": "vl_model.model",
                                "manifest_path": "agents.nodes.missing_worker.config.environment.NEW_MODEL",
                            },
                        ],
                    }
                )
            )
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "apiVersion": "mn.workflow/v1",
                        "kind": "Workflow",
                        "id": "test-workflow",
                        "contract": {},
                        "agents": {"nodes": [
                            {
                                "node_id": "worker",
                                "config": {"environment": {"CUSTOM_MODEL": "keep"}},
                            }
                        ]},
                        "runtime": {},
                    }
                )
            )

            manifest_json, _payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one"},
                "run-1",
            )

        env = json.loads(manifest_json)["flow"]["nodes"][0]["config"]["environment"]
        self.assertEqual(env["CUSTOM_MODEL"], "keep")
        self.assertNotIn("NEW_MODEL", env)

    def test_validate_run_id_rejects_unsafe_values(self):
        with self.assertRaises(HTTPException) as raised:
            validate_run_id("../bad")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "invalid run id")

    def test_cleanup_run_process_uses_recorded_process_group_after_parent_exits(self):
        child_pid: int | None = None
        process_group_id: int | None = None
        proc: subprocess.Popen | None = None
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "run"
            run_dir.mkdir()
            marker = root / "spawned.json"
            spawner = root / "spawn_child.py"
            spawner.write_text(
                "import json\n"
                "import os\n"
                "import subprocess\n"
                "import sys\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                "Path(sys.argv[1]).write_text(json.dumps({\n"
                "    'parent_pid': os.getpid(),\n"
                "    'process_group_id': os.getpgrp(),\n"
                "    'child_pid': child.pid,\n"
                "}))\n"
            )

            try:
                proc = subprocess.Popen([sys.executable, str(spawner), str(marker)], start_new_session=True)
                self.assertTrue(_wait_until(marker.exists), "spawn marker was not written")
                process_info = json.loads(marker.read_text())
                child_pid = int(process_info["child_pid"])
                process_group_id = int(process_info["process_group_id"])
                proc.wait(timeout=5)
                self.assertTrue(_pid_exists(child_pid), "spawned child exited before cleanup")

                (run_dir / "pre_launch_process.json").write_text(json.dumps({
                    "pid": process_info["parent_pid"],
                    "process_group_id": process_group_id,
                }))

                cleanup_run_process(run_dir, "pre_launch_process.json")

                self.assertTrue(
                    _wait_until(lambda: not _pid_exists(child_pid), timeout=8),
                    "cleanup did not stop the recorded process group child",
                )
            finally:
                if process_group_id is not None:
                    try:
                        os.killpg(process_group_id, 9)
                    except OSError:
                        pass
                if child_pid is not None and _pid_exists(child_pid):
                    try:
                        os.kill(child_pid, 9)
                    except OSError:
                        pass
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_cleanup_blueprint_run_processes_collects_recorded_port_listener(self):
        if not shutil.which("lsof"):
            self.skipTest("lsof is required to discover local listener PIDs")
        server: subprocess.Popen | None = None
        port: int | None = None
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runs_root = root / "runs"
            run_dir = runs_root / "run-port-listener"
            run_dir.mkdir(parents=True)
            marker = root / "listener.json"
            server_script = root / "listener.py"
            server_script.write_text(
                "import json\n"
                "import os\n"
                "import socket\n"
                "import sys\n"
                "from pathlib import Path\n"
                "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "sock.bind(('127.0.0.1', 0))\n"
                "sock.listen(5)\n"
                "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'port': sock.getsockname()[1]}))\n"
                "while True:\n"
                "    conn, _addr = sock.accept()\n"
                "    conn.close()\n"
            )

            try:
                server = subprocess.Popen([sys.executable, str(server_script), str(marker)])
                self.assertTrue(_wait_until(marker.exists), "listener marker was not written")
                info = json.loads(marker.read_text())
                port = int(info["port"])
                self.assertTrue(_port_accepts_connection(port), "test listener did not accept connections")
                (run_dir / "post_launch_state.json").write_text(json.dumps({
                    "server_pid": int(info["pid"]),
                    "rtsp_port": port,
                }))

                with patch.dict(os.environ, {
                    "MN_RUNS_ROOT": str(runs_root),
                    "MN_PROCESS_CLEANUP_TIMEOUT_SECONDS": "1",
                }):
                    cleanup_blueprint_run_processes("run-port-listener", reason="test")

                self.assertTrue(
                    _wait_until(lambda: not _pid_exists(int(info["pid"])), timeout=5),
                    "cleanup did not stop the recorded port listener",
                )
                self.assertFalse(_port_accepts_connection(port))
            finally:
                if server is not None and server.poll() is None:
                    server.kill()
                    server.wait(timeout=5)


def test_custom_model_api_selects_most_powerful_capable_node():
    resources = {
        "nodes": [
            {
                "name": "gpu-64",
                "status": "healthy",
                "scheduling_eligible": True,
                "gpu_memory_total_mb": 65536,
            },
            {
                "name": "gpu-128",
                "status": "healthy",
                "scheduling_eligible": True,
                "gpu_memory_total_mb": 131072,
            },
        ]
    }
    systems = {
        "nodes": [
            {
                "name": name,
                "status": "healthy",
                "scheduling_eligible": True,
                "native_sdk_grpc": {
                    "enabled": True,
                    "host": f"{name}.local",
                    "port": 55052,
                    "target": f"{name}.local:55052",
                    "capabilities": ["custom_hf_model_v1"],
                },
            }
            for name in ("gpu-64", "gpu-128")
        ]
    }
    with (
        patch("mn_api.blueprints.runtime_resource_report", return_value=resources),
        patch("mn_api.state.client") as client,
    ):
        client.get_system_summary.return_value = json.dumps(systems)
        placement = blueprints_module.resolve_runtime_cluster_model_for_api(
            requirement={"customize_mode": True},
            entry={"id": "custom", "model": "huggingface.co/acme/custom:Q4_K_M"},
        )

    assert placement["node"] == "gpu-128"
    assert placement["selection"]["gpu_memory_total_mb"] == 131072


def test_custom_model_api_prepares_selected_remote_node():
    runtime_client = unittest.mock.Mock()
    runtime_client.prepare_runtime_model.return_value = json.dumps(
        {
            "status": "installed",
            "docker_model": "huggingface.co/acme/custom:Q4_K_M",
            "gateway": {"host_api_base": "http://127.0.0.1:4000/v1"},
            "endpoint": {
                "model": "huggingface.co/acme/custom:Q4_K_M",
                "runtime_model": "huggingface.co/acme/custom:Q4_K_M",
            },
        }
    )
    cluster = {
        "node": "gpu-128",
        "native_sdk_grpc": {
            "host": "192.168.4.128",
            "port": 55052,
            "target": "192.168.4.128:55052",
        },
    }
    with (
        patch("mn_api.blueprints.Client", return_value=runtime_client) as client_class,
        patch(
            "mn_api.state.refresh_config_from_env",
            return_value=SimpleNamespace(grpc_auth_token="auth", grpc_admin_token="admin"),
        ),
    ):
        result = blueprints_module.install_runtime_cluster_model_for_api(
            requirement={"model": "hf.co/acme/custom:Q4_K_M"},
            entry={
                "id": "huggingface.co/acme/custom:Q4_K_M",
                "model": "huggingface.co/acme/custom:Q4_K_M",
                "api_model": "huggingface.co/acme/custom:Q4_K_M",
                "source_model": "hf.co/acme/custom:Q4_K_M",
                "customize_mode": True,
            },
            model={
                "id": "huggingface.co/acme/custom:Q4_K_M",
                "model": "huggingface.co/acme/custom:Q4_K_M",
            },
            cluster=cluster,
            backend="llama.cpp",
            context_size=4096,
            force=False,
        )

        client_class.assert_called_once_with(
            target="192.168.4.128:55052",
            timeout=blueprints_module.runtime_model_prepare_timeout_seconds(),
            auth_token="auth",
            admin_token="admin",
        )
    payload = runtime_client.prepare_runtime_model.call_args.args[0]
    assert payload["customize_mode"] is True
    assert payload["source_model"] == "hf.co/acme/custom:Q4_K_M"
    assert result["endpoint"]["api_base"] == "http://192.168.4.128:4000/v1"
    assert result["endpoint"]["source"] == "remote_litellm_gateway"

def test_launch_model_policy_is_deferred_without_installing():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        bundle = repo / "vc_assistant"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                    "runtime": {
                        "models": {
                            "primary": {
                                "provider": "docker_model_runner",
                                "model": "default",
                                "required": True,
                            }
                        }
                    },
                    "nodes": [],
                }
            ),
            encoding="utf-8",
        )
        resource_report = {
            "nodes": [
                {
                    "name": "local",
                    "status": "healthy",
                    "scheduling_eligible": True,
                    "devices": [
                        {
                            "kind": "gpu",
                            "type": "integrated_gpu",
                            "vendor": "apple",
                            "memory_total_mb": 16384,
                        }
                    ],
                }
            ]
        }
        with patch("mn_api.blueprints.install_model_entry") as install, patch(
            "mn_api.blueprints.runtime_resource_report",
            return_value=resource_report,
        ):
            summary = defer_blueprint_runtime_models(
                repo,
                {"id": "vc_assistant", "path": "vc_assistant"},
            )

    install.assert_not_called()
    assert summary["deferred"] is True
    assert summary["models"][0]["selection_policy"] == [
        "nemotron-3.5-lightning:latest",
        "gemma4:e2b",
    ]
    assert summary["env"]["MN_RUNTIME_MODEL_MANAGED"] == "1"


def test_launch_model_policy_keeps_provider_default_out_of_managed_dmr(tmp_path, monkeypatch):
    mn_home = tmp_path / "mn-home"
    monkeypatch.setenv("MN_HOME", str(mn_home))
    add_registered_models(
        [
            provider_registration(
                "muse",
                source_model="muse-upstream",
                api_base="http://spark:8000/v1",
            )
        ]
    )
    set_registered_default_model("muse")
    bundle = tmp_path / "vc_assistant"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                "config": {"manifest_defaults": ["llm"]},
                "llm": {
                    "model": "default",
                    "configs": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "api_base": "auto",
                        }
                    },
                },
                "runtime": {
                    "models": {
                        "primary": {
                            "provider": "docker_model_runner",
                            "model": "default",
                            "required": True,
                        }
                    }
                },
                "nodes": [],
            }
        ),
        encoding="utf-8",
    )

    summary = defer_blueprint_runtime_models(
        tmp_path,
        {"id": "vc_assistant", "path": "vc_assistant"},
    )

    assert summary["ok"] is True
    assert summary["models"][0]["provider"] == "litellm_proxy"
    assert summary["models"][0]["selection_policy"] == ["muse"]
    assert summary["env"]["MN_LLM_MODEL"] == "default"
    assert "MN_RUNTIME_MODEL_MANAGED" not in summary["env"]


def test_launch_model_policy_skips_runtime_models_for_fake_llm():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        bundle = repo / "fake_assistant"
        bundle.mkdir()
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "test-workflow", "contract": {}, "agents": {}, "runtime": {},
                    "runtime": {
                        "models": {
                            "primary": {
                                "provider": "docker_model_runner",
                                "model": "default",
                                "required": True,
                            }
                        }
                    },
                    "nodes": [],
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "mn_api.blueprints.runtime_resource_report",
            side_effect=AssertionError("fake launches must not inspect Model Runner resources"),
        ):
            summary = defer_blueprint_runtime_models(
                repo,
                {"id": "fake_assistant", "path": "fake_assistant"},
                config_overrides={"llm": {"mode": "fake"}},
            )

    assert summary["ok"] is True
    assert summary["models"] == []
    assert summary["env"]["MN_LLM_PROVIDER"] == "fake"
