import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from mn_api.blueprints import (
    blueprint_bundle_root,
    load_blueprint_bundle,
    load_blueprint_catalog,
    validate_run_id,
)


class TestBlueprintServices(unittest.TestCase):
    def test_catalog_accepts_wrapped_index_and_normalizes_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "index.json").write_text(
                json.dumps(
                    {
                        "blueprints": [
                            {
                                "blueprintId": "worker.two",
                                "product": {
                                    "name": "Worker Two",
                                    "one_line": "Does useful work.",
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
        self.assertEqual(blueprints[0]["pricing"], {"model": "metered", "rate": 12.5, "unit": "run"})
        self.assertEqual(blueprints[0]["rate_label"], "$12.5/run")
        self.assertEqual(blueprints[0]["runtime_features"], ["streams"])
        self.assertEqual(blueprints[0]["capabilities"], [])

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
            (payloads / "input.txt").write_bytes(b"hello")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "graph_id": "worker_graph",
                        "metadata": "replace-me",
                    }
                )
            )

            manifest_json, payload_bytes = load_blueprint_bundle(
                repo.resolve(),
                {"id": "worker_one", "path": "worker_one", "revision": "rev-7"},
                "run-7",
            )

        manifest = json.loads(manifest_json)
        self.assertEqual(manifest["run_id"], "run-7")
        self.assertEqual(manifest["metadata"]["blueprint_id"], "worker_one")
        self.assertEqual(manifest["metadata"]["blueprint_revision"], "rev-7")
        self.assertEqual(payload_bytes, {"nested/input.txt": b"hello"})

    def test_validate_run_id_rejects_unsafe_values(self):
        with self.assertRaises(HTTPException) as raised:
            validate_run_id("../bad")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "invalid run id")
