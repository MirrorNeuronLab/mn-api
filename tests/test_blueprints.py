import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from mn_api.blueprints import (
    blueprint_bundle_root,
    filter_blueprints_by_category,
    load_blueprint_categories,
    load_blueprint_bundle,
    load_blueprint_catalog,
    validate_run_id,
)


class TestBlueprintServices(unittest.TestCase):
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
