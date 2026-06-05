import json
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from mn_api.blueprints import (
    blueprint_bundle_root,
    cached_git_repo_path,
    cleanup_blueprint_run_processes,
    cleanup_run_process,
    ensure_git_blueprint_repo,
    filter_blueprints_by_category,
    inject_local_blueprint_support_path,
    install_blueprint_runtime_models,
    is_git_repo_url,
    load_blueprint_categories,
    load_blueprint_bundle,
    load_blueprint_catalog,
    runtime_blueprint_environment_overrides,
    validate_run_id,
)

requires_blueprint_support = unittest.skipIf(
    importlib.util.find_spec("mn_blueprint_support") is None,
    "mn_blueprint_support is not installed",
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


class TestBlueprintServices(unittest.TestCase):
    def test_is_git_repo_url_accepts_common_remote_forms(self):
        self.assertTrue(is_git_repo_url("https://github.com/MirrorNeuronLab/otterdesk-blueprints.git"))
        self.assertTrue(is_git_repo_url("ssh://git@github.com/MirrorNeuronLab/otterdesk-blueprints.git"))
        self.assertTrue(is_git_repo_url("git@github.com:MirrorNeuronLab/otterdesk-blueprints.git"))
        self.assertFalse(is_git_repo_url("/Users/homer/Projects/mirror-neuron-set/otterdesk-blueprints"))

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

    def test_catalog_accepts_wrapped_index_and_normalizes_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            manifest_dir = repo / "worker.two"
            manifest_dir.mkdir()
            (manifest_dir / "manifest.json").write_text(
                json.dumps(
                    {
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

            repo_root, blueprints = load_blueprint_catalog(SimpleNamespace(blueprint_repo=str(repo)))

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

    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.subprocess.run")
    def test_install_blueprint_runtime_models_passes_backend_and_context(self, mock_run, mock_installed, mock_ledger, mock_record):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        mock_installed.return_value = False
        mock_ledger.return_value = {"version": 1, "models": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
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
        command = mock_run.call_args.args[0]
        self.assertIn("model", command)
        self.assertIn("install", command)
        self.assertIn("gemma4:e2b", command)
        self.assertIn("--backend", command)
        self.assertIn("llama.cpp", command)
        self.assertIn("--context-size", command)
        self.assertIn("2048", command)
        mock_record.assert_called_once()

    @patch("mn_api.blueprints.record_model_owner")
    @patch("mn_api.blueprints.load_model_ownership")
    @patch("mn_api.blueprints.docker_model_installed")
    @patch("mn_api.blueprints.subprocess.run")
    def test_install_blueprint_runtime_models_failure_does_not_record_owner(self, mock_run, mock_installed, mock_ledger, mock_record):
        mock_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="pull failed")
        mock_installed.return_value = False
        mock_ledger.return_value = {"version": 1, "models": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "worker_one"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(json.dumps({
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
        self.assertIn("install", mock_run.call_args.args[0])
        mock_record.assert_not_called()

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

            repo_root, blueprints = load_blueprint_catalog(SimpleNamespace(blueprint_repo=str(repo)))
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
                load_blueprint_catalog(SimpleNamespace(blueprint_repo=str(repo)))

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
                            "manifest_path": "nodes.worker.config.environment.CUSTOM_MODEL",
                        }
                    ],
                })
            )
            (config_dir / "overwrite.json").write_text(json.dumps({"vl_model": {"model": "overwrite"}}))
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "graph_id": "worker_graph",
                        "nodes": [
                            {
                                "node_id": "worker",
                                "config": {"environment": {"LITELLM_MODEL": "ollama/test"}},
                            }
                        ],
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
        env = manifest["nodes"][0]["config"]["environment"]
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(injected_config["vl_model"], {"model": "overwrite", "base_url": "http://local"})
        self.assertEqual(env["VL_MODEL_NAME"], "overwrite")
        self.assertEqual(env["OLLAMA_MODEL"], "overwrite")
        self.assertEqual(env["VL_MODEL_BASE_URL"], "http://local")
        self.assertEqual(env["CUSTOM_MODEL"], "overwrite")
        self.assertEqual(env["MN_LLM_MODEL"], "ollama/test")
        self.assertEqual(payload_bytes, {"nested/input.txt": b"hello"})

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
                        "graph_id": "worker_graph",
                        "nodes": [
                            {
                                "node_id": "worker",
                                "config": {
                                    "runner_module": "MirrorNeuron.Runner.HostLocal",
                                    "environment": {},
                                },
                            }
                        ],
                    }
                )
            )

            manifest_json, _payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one"},
                "run-7",
            )

        manifest = json.loads(manifest_json)
        env = manifest["nodes"][0]["config"]["environment"]
        self.assertEqual(env["MN_LLM_PROVIDER"], "docker_model_runner")
        self.assertEqual(env["MN_LLM_MODEL"], "ai/gemma4:E2B")
        self.assertEqual(env["MN_LLM_API_BASE"], "http://localhost:12434/engines/v1")

    @requires_blueprint_support
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
                        "graph_id": "tax_worker",
                        "nodes": [
                            {
                                "node_id": "document_intake_agent",
                                "config": {"environment": {}},
                            }
                        ],
                        "metadata": {},
                    }
                )
            )

            manifest_json, payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "tax_worker", "path": "tax_worker"},
                "tax-run-1",
                config_overrides={"tax_documents": {"folder_path": str(host_docs)}},
            )

        manifest = json.loads(manifest_json)
        env = manifest["nodes"][0]["config"]["environment"]
        injected_config = json.loads(env["MN_BLUEPRINT_CONFIG_JSON"])
        self.assertEqual(injected_config["tax_documents"]["folder_path"], "mn_local_inputs/tax_documents")
        self.assertEqual(injected_config["inputs"]["payload"]["document_folder"], "mn_local_inputs/tax_documents")
        self.assertEqual(
            payload_bytes["tax_workflow/mn_local_inputs/tax_documents/w2.txt"],
            b"box 1 wages 100\n",
        )
        self.assertNotIn("tax_workflow/mn_local_inputs/tax_documents/ignore.csv", payload_bytes)
        self.assertEqual(manifest["metadata"]["mn_local_inputs"]["folders"][0]["file_count"], 1)

    @requires_blueprint_support
    def test_load_blueprint_bundle_injects_runtime_web_ui_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            bundle = repo / "video_watch_assistant"
            payloads = bundle / "payloads"
            payloads.mkdir(parents=True)
            config_dir = bundle / "config"
            config_dir.mkdir()
            (config_dir / "default.json").write_text(
                json.dumps(
                    {
                        "identity": {"blueprint_id": "video_watch_assistant", "name": "Video Watch"},
                        "web_ui": {
                            "enabled": True,
                            "output": {"adapter": "gradio", "title": "Video Dashboard"},
                            "dashboard": {
                                "browser_video_source": "http://127.0.0.1:8889/video-watch/"
                            },
                        },
                    }
                )
            )
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": "1.0",
                        "type": "service",
                        "graph_id": "video_watch_assistant_v1",
                        "nodes": [
                            {
                                "node_id": "worker",
                                "agent_type": "executor",
                                "config": {"environment": {}},
                            }
                        ],
                        "entrypoints": ["worker"],
                        "initial_inputs": {"worker": [{}]},
                        "metadata": {},
                    }
                )
            )

            inject_local_blueprint_support_path()
            with patch.dict(
                os.environ,
                {
                    "MN_BLUEPRINT_WEB_UI_PORT_START": "61000",
                    "MN_BLUEPRINT_WEB_UI_PORT_END": "61001",
                    "MN_BLUEPRINT_WEB_UI_PORT_ALLOCATION_MODE": "prepublished",
                },
            ), patch("mn_blueprint_support.runtime_web_ui.web_ui_port_available", return_value=False):
                manifest_json, payload_bytes = load_blueprint_bundle(
                    repo.resolve(),
                    {"id": "video_watch_assistant", "path": "video_watch_assistant"},
                    "video-run-7",
                    web_ui_reserved_ports={61000},
                )

        manifest = json.loads(manifest_json)
        web_ui_node = next(node for node in manifest["nodes"] if node["node_id"] == "web_ui_dashboard")
        self.assertIn("web_ui_dashboard", manifest["entrypoints"])
        self.assertEqual(web_ui_node["resources"]["ports"][0]["port"], 61001)
        self.assertEqual(web_ui_node["services"][0]["name"], "blueprint-web-ui")
        self.assertEqual(web_ui_node["services"][0]["meta"]["run_id"], "video-run-7")
        self.assertEqual(
            web_ui_node["services"][0]["meta"]["browser_video_source"],
            "http://127.0.0.1:8889/video-watch/",
        )
        self.assertEqual(
            manifest["metadata"]["blueprint_web_ui_service"]["url"],
            "http://localhost:61001",
        )
        self.assertIn(
            "mn_runtime_web_ui/src/mn_blueprint_support/gradio_dashboard.py",
            payload_bytes,
        )

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
                    "graph_id": "worker_graph",
                    "nodes": [
                        {
                            "node_id": "worker",
                            "config": {
                                "runner_module": "MirrorNeuron.Sandbox.OpenShell",
                                "custom_openshell_image": "worker/openshell_sandbox",
                            },
                        }
                    ],
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
        config = manifest["nodes"][0]["config"]
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
                        "nodes": [
                            {
                                "node_id": "worker",
                                "config": {"environment": {}},
                            }
                        ]
                    }
                )
            )

            manifest_json, _payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one"},
                "run-1",
            )

        env = json.loads(manifest_json)["nodes"][0]["config"]["environment"]
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
                    (bundle / "manifest.json").write_text(json.dumps({"nodes": []}))

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
                                "manifest_path": "nodes.worker.config.environment.CUSTOM_MODEL",
                            },
                            {
                                "config_path": "vl_model.model",
                                "manifest_path": "nodes.missing_worker.config.environment.NEW_MODEL",
                            },
                        ],
                    }
                )
            )
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "node_id": "worker",
                                "config": {"environment": {"CUSTOM_MODEL": "keep"}},
                            }
                        ]
                    }
                )
            )

            manifest_json, _payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one"},
                "run-1",
            )

        env = json.loads(manifest_json)["nodes"][0]["config"]["environment"]
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
