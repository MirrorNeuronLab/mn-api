from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mn_api.run_outputs import output_path_by_index, output_refs


class TestRunOutputs(unittest.TestCase):
    def test_output_refs_collect_nested_payloads_dedupe_and_skip_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-output"
            output_dir = root / "outputs"
            run_dir.mkdir(parents=True)
            output_dir.mkdir()
            report = output_dir / "customer report.md"
            packet = output_dir / "packet.pdf"
            summary = output_dir / "summary.json"
            log = output_dir / "debug.log"
            report.write_text("# Report\n", encoding="utf-8")
            packet.write_bytes(b"%PDF-1.4")
            summary.write_text('{"ok": true}', encoding="utf-8")
            log.write_text("done\n", encoding="utf-8")

            (run_dir / "post_launch_materialized.json").write_text(
                json.dumps(
                    {
                        "output_files": [
                            {"kind": "customer_report", "path": str(report)},
                            {"kind": "duplicate_report", "path": str(report)},
                            {"kind": "missing", "path": str(output_dir / "missing.txt")},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "post_launch_state.json").write_text(
                json.dumps({"result": {"output_files": [{"local_path": str(log)}]}}),
                encoding="utf-8",
            )
            (run_dir / "result.json").write_text(
                json.dumps(
                    {
                        "final_artifact": {
                            "output_files": [
                                {"kind": "summary_json", "file_path": str(summary)}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "final_artifact.json").write_text(
                json.dumps({"output_files": [str(packet)]}),
                encoding="utf-8",
            )

            refs = output_refs("run-output", run_dir)

        self.assertEqual(
            [ref["path"] for ref in refs],
            [str(path.resolve()) for path in (report, log, summary, packet)],
        )
        self.assertEqual(
            [ref["artifact_id"] for ref in refs],
            [
                "output_0_customer_report",
                "output_1_debug",
                "output_2_summary_json",
                "output_3_packet",
            ],
        )
        self.assertEqual(refs[0]["content_type"], "text/markdown; charset=utf-8")
        self.assertEqual(refs[1]["content_type"], "text/plain; charset=utf-8")
        self.assertEqual(refs[2]["content_type"], "application/json")
        self.assertEqual(refs[3]["content_type"], "application/pdf")
        self.assertEqual(refs[0]["url"], "/api/v1/runs/run-output/outputs/0")
        self.assertNotIn("reveal_url", refs[0])
        self.assertTrue(all(ref["external"] for ref in refs))

    def test_output_path_by_index_resolves_valid_indexes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-output"
            output_dir = root / "outputs"
            run_dir.mkdir(parents=True)
            output_dir.mkdir()
            report = output_dir / "report.md"
            report.write_text("# Report\n", encoding="utf-8")
            (run_dir / "final_artifact.json").write_text(
                json.dumps({"output_files": [{"path": str(report)}]}),
                encoding="utf-8",
            )

            self.assertEqual(output_path_by_index(run_dir, 0), report.resolve())
            self.assertIsNone(output_path_by_index(run_dir, -1))
            self.assertIsNone(output_path_by_index(run_dir, 1))


if __name__ == "__main__":
    unittest.main()
