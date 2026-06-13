from __future__ import annotations

import unittest

from mn_api.job_activity import compact_event, compact_value, enrich_workflow_progress_activity


class TestJobActivity(unittest.TestCase):
    def test_compact_value_truncates_strings_lists_and_blob_keys(self):
        compact = compact_value(
            {
                "stdout": "x" * 2500,
                "items": list(range(30)),
                "nested": {"ok": True},
            }
        )

        self.assertEqual(compact["stdout"]["type"], "string")
        self.assertTrue(compact["stdout"]["omitted"])
        self.assertEqual(compact["stdout"]["chars"], 2500)
        self.assertEqual(len(compact["items"]), 26)
        self.assertEqual(compact["items"][-1], {"omitted_items": 5})
        self.assertEqual(compact["nested"], {"ok": True})

    def test_compact_event_keeps_summary_fields_and_compacts_payload(self):
        event = {
            "type": "tool_call",
            "timestamp": "2026-01-01T00:00:00Z",
            "agent_id": "agent-1",
            "payload": {
                "message": "finished",
                "content": "secret" * 1000,
            },
        }

        compact = compact_event(event)

        self.assertEqual(compact["type"], "tool_call")
        self.assertEqual(compact["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(compact["agent_id"], "agent-1")
        self.assertEqual(compact["payload"]["message"], "finished")
        self.assertTrue(compact["payload"]["content"]["omitted"])

    def test_enrich_workflow_progress_activity_adds_step_and_agent_activity(self):
        snapshot = {
            "steps": [
                {
                    "id": "research",
                    "agents": [{"id": "research:worker"}],
                }
            ],
            "current_step": {"id": "research", "current": True},
        }
        events = [
            {
                "type": "workflow_step_attempt_started",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {
                    "worker": "worker",
                    "message": "Reading source documents",
                    "details": {"content": "raw transcript" * 1000},
                },
            }
        ]

        enrich_workflow_progress_activity(snapshot, events)

        step = snapshot["steps"][0]
        agent = step["agents"][0]
        self.assertEqual(step["last_activity"]["message"], "Reading source documents")
        self.assertEqual(step["last_activity"]["step_id"], "research")
        self.assertEqual(step["activity_summary"], "Reading source documents")
        self.assertEqual(agent["last_activity"]["agent_id"], "worker")
        self.assertEqual(agent["activity_summary"], "Reading source documents")
        self.assertEqual(snapshot["current_step"]["last_activity"]["message"], "Reading source documents")
        self.assertTrue(step["last_activity"]["details"]["content"]["omitted"])


if __name__ == "__main__":
    unittest.main()
