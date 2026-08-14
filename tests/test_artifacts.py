from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mn_api.artifacts import (
    artifact_content_type,
    artifact_id,
    artifact_ref,
    list_artifact_files,
)


class TestArtifacts(unittest.TestCase):
    def test_artifact_ref_builds_stable_metadata_for_known_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "run:one"
            run_dir.mkdir(parents=True)
            path = run_dir / "result.json"
            content = b'{"ok": true}\n'
            path.write_bytes(content)

            ref = artifact_ref("run:one", path, run_dir)

        self.assertEqual(ref["artifact_id"], "result_json")
        self.assertEqual(ref["relative_path"], "result.json")
        self.assertEqual(ref["size_bytes"], len(content))
        self.assertEqual(ref["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(ref["content_type"], "application/json")
        self.assertEqual(ref["url"], "/api/v1/runs/run%3Aone/artifacts/result.json")
        self.assertNotIn("reveal_url", ref)

    def test_artifact_id_handles_rotated_and_nested_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            rotated = run_dir / "archive" / "errors.001.jsonl.gz"
            report = run_dir / "nested output" / "review packet.md"
            rotated.parent.mkdir()
            report.parent.mkdir()
            rotated.write_text("{}", encoding="utf-8")
            report.write_text("# Report\n", encoding="utf-8")

            rotated_id = artifact_id(rotated, run_dir)
            report_ref = artifact_ref("run with spaces", report, run_dir)

        self.assertEqual(rotated_id, "errors_jsonl_001")
        self.assertEqual(report_ref["artifact_id"], "nested_output_review_packet_md")
        self.assertEqual(
            report_ref["url"],
            "/api/v1/runs/run%20with%20spaces/artifacts/nested%20output/review%20packet.md",
        )

    def test_list_artifact_files_keeps_supported_files_in_stable_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / ".hidden.json").write_text("{}", encoding="utf-8")
            (run_dir / "notes.bin").write_bytes(b"ignored")
            (run_dir / "report.md").write_text("# Report\n", encoding="utf-8")
            (run_dir / "nested").mkdir()
            (run_dir / "nested" / "events.002.jsonl.gz").write_text("{}", encoding="utf-8")
            (run_dir / "result.json").write_text("{}", encoding="utf-8")

            paths = list_artifact_files(run_dir)

        self.assertEqual(
            [path.relative_to(run_dir).as_posix() for path in paths],
            ["nested/events.002.jsonl.gz", "report.md", "result.json"],
        )

    def test_artifact_content_type_falls_back_for_unknown_extensions(self):
        self.assertEqual(artifact_content_type(Path("report.md")), "text/markdown; charset=utf-8")
        self.assertEqual(artifact_content_type(Path("payload.unknown")), "application/octet-stream")


if __name__ == "__main__":
    unittest.main()
