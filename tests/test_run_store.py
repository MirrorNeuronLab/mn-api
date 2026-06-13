from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mn_api.run_store import read_json_file, read_jsonl_file, run_dir_from_id, stream_jsonl_files


class TestRunStore(unittest.TestCase):
    def test_run_dir_from_id_validates_id_and_root_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "run-1"
            existing.mkdir()

            with patch.dict(os.environ, {"MN_RUNS_ROOT": str(root)}):
                self.assertEqual(run_dir_from_id("run-1"), existing.resolve())
                self.assertEqual(run_dir_from_id("future-run", must_exist=False), (root / "future-run").resolve())
                self.assertIsNone(run_dir_from_id("missing-run"))
                self.assertIsNone(run_dir_from_id("../run-1", must_exist=False))
                self.assertIsNone(run_dir_from_id("bad/run", must_exist=False))
                self.assertIsNone(run_dir_from_id(""))

    def test_read_json_file_handles_missing_invalid_and_non_object_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.json"
            malformed = root / "malformed.json"
            non_object = root / "non-object.json"
            invalid_encoding = root / "invalid-encoding.json"
            valid.write_text(json.dumps({"ok": True}), encoding="utf-8")
            malformed.write_text("{not json", encoding="utf-8")
            non_object.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
            invalid_encoding.write_bytes(b"\xff")

            self.assertEqual(read_json_file(root / "missing.json"), {})
            self.assertEqual(read_json_file(valid), {"ok": True})
            self.assertEqual(read_json_file(malformed), {})
            self.assertEqual(read_json_file(non_object), {})
            self.assertEqual(read_json_file(invalid_encoding), {})

            with self.assertRaises(RuntimeError) as raised:
                read_json_file(malformed, raise_on_error=True, error_detail="bad json")

        self.assertEqual(str(raised.exception), "bad json")

    def test_read_jsonl_file_tails_dict_events_and_marks_unparseable_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "first"}),
                        "not-json",
                        json.dumps(["not", "an", "event"]),
                        json.dumps({"type": "second"}),
                        json.dumps({"type": "third"}),
                    ]
                ),
                encoding="utf-8",
            )

            events = read_jsonl_file(path, limit=3)

        self.assertEqual([event["type"] for event in events], ["unparseable_event", "second", "third"])
        self.assertEqual(events[0]["payload"], {"line": "not-json"})

    def test_read_jsonl_file_reads_gzip_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "compressed"}) + "\n")

            self.assertEqual(read_jsonl_file(path), [{"type": "compressed"}])

    def test_stream_jsonl_files_resolves_index_segments_and_compressed_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            segments = run_dir / "segments"
            segments.mkdir()
            first = segments / "first.jsonl"
            second = segments / "second.jsonl"
            first.write_text("", encoding="utf-8")
            second.with_suffix(".jsonl.gz").write_bytes(b"")
            (run_dir / "events.index.json").write_text(
                json.dumps({"segments": [{"path": "segments/first.jsonl"}, {"path": "segments/second.jsonl"}]}),
                encoding="utf-8",
            )

            paths = stream_jsonl_files(run_dir, "events.jsonl")

        self.assertEqual(paths, [first, second.with_suffix(".jsonl.gz"), run_dir / "events.jsonl"])


if __name__ == "__main__":
    unittest.main()
