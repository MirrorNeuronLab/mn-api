from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from mn_api.routes import runs


def test_run_compare_payload_uses_result_final_artifact_fallback():
    payload = runs._run_compare_payload(
        "a",
        {"run": {"run_id": "a", "status": "completed"}, "result": {"final_artifact": {"score": 1}}, "events": [{}]},
        "b",
        {"run": {"run_id": "b", "status": "failed"}, "final_artifact": {"score": 2, "nested": {"x": 1}}},
    )

    assert payload["runs"]["a"]["event_count"] == 1
    assert payload["artifact_diff"] == {"score": {"a": 1, "b": 2}}


def test_markdown_and_duration_helpers():
    assert runs._markdown_cell("a|b\nc") == "a\\|b c"
    assert runs._duration_seconds("2m") == 120
    assert runs._duration_seconds("1.5h") == 5400
    assert runs._duration_seconds("2d") == 172800
    assert runs._duration_seconds("30") == 30
    with pytest.raises(HTTPException):
        runs._duration_seconds("bad")
    with pytest.raises(HTTPException):
        runs._duration_seconds("1w")


def test_render_markdown_export_contains_stable_sections():
    rendered = runs._render_markdown_export(
        {
            "run": {"run_id": "run-1", "blueprint_id": "bp", "status": "completed"},
            "final_artifact": {"score": 1},
            "events": [{"type": "done"}],
        }
    )

    assert "# Blueprint Run run-1" in rendered
    assert "## Final Artifact" in rendered
    assert json.dumps({"score": 1}, indent=2, sort_keys=True) in rendered
