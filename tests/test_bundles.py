import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from mn_api.bundles import find_bundle_root, load_uploaded_bundle, restore_exported_rag_db, safe_extract_path


class TestBundleServices(unittest.TestCase):
    def test_load_uploaded_bundle_reads_payload_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root = Path(tmpdir)
            bundle_root = upload_root / "bundle_123"
            payloads = bundle_root / "payloads" / "nested"
            payloads.mkdir(parents=True)
            (bundle_root / "manifest.json").write_text(
                '{"apiVersion": "mn.workflow/v2", "graph_id": "g"}'
            )
            (payloads / "a.txt").write_bytes(b"hello")

            manifest_json, payload_bytes = load_uploaded_bundle(str(bundle_root), upload_root)

        self.assertEqual(
            manifest_json,
            '{"apiVersion": "mn.workflow/v2", "graph_id": "g"}',
        )
        self.assertEqual(payload_bytes, {"nested/a.txt": b"hello"})

    def test_load_uploaded_bundle_rejects_paths_outside_upload_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root = Path(tmpdir) / "uploads"
            outside = Path(tmpdir) / "outside"
            outside.mkdir()

            with self.assertRaises(HTTPException) as raised:
                load_uploaded_bundle(str(outside), upload_root)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "unknown uploaded bundle")

    def test_safe_extract_path_rejects_absolute_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            for member_name in ("/tmp/manifest.json", "../manifest.json"):
                with self.subTest(member_name=member_name):
                    with self.assertRaises(HTTPException) as raised:
                        safe_extract_path(root, member_name)
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertEqual(raised.exception.detail, "bundle contains unsafe paths")

    def test_find_bundle_root_accepts_single_nested_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extracted_root = Path(tmpdir)
            nested = extracted_root / "bundle-root"
            nested.mkdir()
            (nested / "manifest.json").write_text('{"apiVersion": "mn.workflow/v2"}')

            self.assertEqual(find_bundle_root(extracted_root), nested)

    def test_restore_exported_rag_db_copies_runtime_cache_to_mn_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundle_root = root / "bundle-root"
            db_dir = bundle_root / "__mn_runtime" / "rag" / "mirror_neuron_rag"
            db_dir.mkdir(parents=True)
            (db_dir / "worker_one.db").write_bytes(b"milvus-lite-cache")
            (db_dir / "worker_one.db-wal").write_bytes(b"wal")
            export_manifest = {
                "schema_version": "mn.rag_export.v1",
                "backend": "milvus_lite",
                "blueprint_id": "worker_one",
                "namespace": "mirror_neuron_rag",
                "db_path": "mirror_neuron_rag/worker_one.db",
                "files": [
                    {"path": "__mn_runtime/rag/mirror_neuron_rag/worker_one.db", "size": 18},
                    {"path": "__mn_runtime/rag/mirror_neuron_rag/worker_one.db-wal", "size": 3},
                ],
            }
            (bundle_root / "__mn_runtime" / "rag" / "manifest.json").write_text(json.dumps(export_manifest))
            rag_root = root / "rag-root"

            result = restore_exported_rag_db(
                bundle_root,
                manifest={"metadata": {"blueprint_id": "worker_one"}},
                env={"MN_RAG_DB_ROOT": str(rag_root)},
            )

            expected_db = rag_root.resolve() / "mirror_neuron_rag" / "worker_one.db"
            self.assertTrue(result["imported"])
            self.assertEqual(result["path"], str(expected_db))
            self.assertEqual(expected_db.read_bytes(), b"milvus-lite-cache")
            self.assertEqual(Path(f"{expected_db}-wal").read_bytes(), b"wal")
