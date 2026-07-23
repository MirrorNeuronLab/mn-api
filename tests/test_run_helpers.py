from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from mn_api.routes import runs


def test_read_event_tail_handles_unparseable_lines_and_limits(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text('{"type":"one"}\nnot-json\n{"type":"two"}\n', encoding="utf-8")

    assert runs._read_event_tail(events, limit=2) == [
        {"type": "unparseable_event", "payload": {"line": "not-json"}},
        {"type": "two"},
    ]
    assert runs._read_event_tail(events, limit=0) == []
    assert runs._read_event_tail(tmp_path / "missing.jsonl", limit=10) == []


def test_video_source_helpers_resolve_and_constrain_paths(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    ui = {
        "components": [{"type": "text"}, {"type": "video", "source": "clip.mp4"}],
        "metadata": {"bundle_dir": str(bundle_dir)},
    }

    assert runs._first_video_source(ui) == "clip.mp4"
    assert runs._local_source_path("clip.mp4", run_dir) == (run_dir / "clip.mp4").resolve()
    assert runs._local_source_path("file:///tmp/video.mp4", run_dir) == Path("/tmp/video.mp4").resolve()
    assert runs._local_source_path("https://example.com/video.mp4", run_dir) is None
    roots = runs._allowed_local_roots(run_dir, ui)
    assert runs._is_allowed_local_path(run_dir / "clip.mp4", roots) is True
    assert runs._is_allowed_local_path(bundle_dir / "clip.mp4", roots) is True
    assert runs._is_allowed_local_path(tmp_path / "other.mp4", roots) is False


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
