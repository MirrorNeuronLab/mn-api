import json
import tempfile
import unittest
from pathlib import Path

from mn_api.agent_graph import build_agent_graph, event_message_summary, load_manifest_for_job


class TestAgentGraphServices(unittest.TestCase):
    def test_build_agent_graph_marks_manifest_edges_observed_by_events(self):
        details = {
            "job": {
                "job_id": "job-1",
                "graph_id": "graph-1",
                "status": "running",
                "topology": {
                    "nodes": [
                        {"node_id": "source", "agent_type": "executor"},
                        {"node_id": "sink", "agent_type": "executor"},
                    ],
                    "edges": [
                        {
                            "edge_id": "source_to_sink",
                            "from_node": "source",
                            "to_node": "sink",
                            "message_type": "task",
                        }
                    ],
                },
            },
            "agents": [],
        }
        events = [
            {
                "type": "agent_message_received",
                "timestamp": "2026-04-29T12:00:00Z",
                "payload": {"from": "source", "to": "sink", "type": "task"},
            }
        ]

        graph = build_agent_graph("job-1", details, events)

        self.assertEqual(graph["stats"]["agent_count"], 2)
        self.assertEqual(graph["stats"]["message_count"], 1)
        self.assertEqual(graph["edges"][0]["source_event"], "manifest+events")
        self.assertEqual(graph["edges"][0]["last_seen_at"], "2026-04-29T12:00:00Z")

    def test_event_message_summary_supports_backpressure_and_envelopes(self):
        backpressure = {
            "type": "backpressure_signal",
            "payload": {"from": "router", "to": "worker", "type": "slow_down"},
        }
        envelope = {
            "type": "other",
            "message": {"envelope": {"from": "planner", "to": "worker", "type": "task"}},
        }

        self.assertEqual(event_message_summary(backpressure), backpressure["payload"])
        self.assertEqual(event_message_summary(envelope), envelope["message"]["envelope"])
        self.assertIsNone(event_message_summary({"type": "agent_message_received", "payload": "bad"}))

    def test_load_manifest_for_job_returns_empty_dict_for_missing_or_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.json"
            scalar = Path(tmpdir) / "scalar.json"
            malformed = Path(tmpdir) / "malformed.json"
            scalar.write_text(json.dumps(["not", "an", "object"]))
            malformed.write_text("{not json")

            self.assertEqual(load_manifest_for_job({"manifest_ref": {"manifest_path": str(missing)}}), {})
            self.assertEqual(load_manifest_for_job({"manifest_ref": {"manifest_path": str(scalar)}}), {})
            self.assertEqual(load_manifest_for_job({"manifest_ref": {"manifest_path": str(malformed)}}), {})
