import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from mn_api.bundles import find_bundle_root, load_uploaded_bundle, safe_extract_path


class TestBundleServices(unittest.TestCase):
    def test_load_uploaded_bundle_reads_payload_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root = Path(tmpdir)
            bundle_root = upload_root / "bundle_123"
            payloads = bundle_root / "payloads" / "nested"
            payloads.mkdir(parents=True)
            (bundle_root / "manifest.json").write_text('{"graph_id": "g"}')
            (payloads / "a.txt").write_bytes(b"hello")

            manifest_json, payload_bytes = load_uploaded_bundle(str(bundle_root), upload_root)

        self.assertEqual(manifest_json, '{"graph_id": "g"}')
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
            (nested / "manifest.json").write_text("{}")

            self.assertEqual(find_bundle_root(extracted_root), nested)
