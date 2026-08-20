from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from mn_api import state
from mn_api.routes import jobs


def test_decode_manifest(monkeypatch):
    executable = {
        "apiVersion": "mn.workflow/v1",
        "kind": "Workflow",
        "id": "g",
        "contract": {},
        "agents": {},
        "runtime": {},
    }
    assert jobs._decode_manifest(json.dumps(executable)) == executable
    with pytest.raises(HTTPException):
        jobs._decode_manifest("{bad")
    with pytest.raises(HTTPException):
        jobs._decode_manifest("[]")

    monkeypatch.setattr("mn_api.routes.jobs.manifest_form", lambda manifest: "source")
    monkeypatch.setattr(
        "mn_api.routes.jobs.expand_manifest_source", lambda manifest, root_dir=None: {"expanded": root_dir}
    )
    assert jobs._decode_manifest('{"source":"x"}', root_dir="root") == {"expanded": "root"}


def test_job_status_and_failure_helpers():
    assert jobs._is_success_status("Succeeded!") is True

    snapshot = {"status": "completed", "failure": {"message": "old"}}
    jobs._clear_success_failure(snapshot)
    assert "failure" not in snapshot

    assert jobs._infer_status([{"type": "job_failed"}], {}, {}) == "failed"
    assert jobs._infer_status([{"type": "workflow_completed"}], {}, {}) == "completed"
    assert jobs._infer_status([{"type": "job_paused"}], {}, {}) == "paused"
    assert jobs._infer_status([{"type": "job_status", "payload": {"status": "waiting"}}], {}, {}) == "waiting"


def test_stream_job_events_handles_bad_json_and_stream_errors(monkeypatch):
    class FakeClient:
        def stream_events(self, *_args, **_kwargs):
            yield '{"type":"job_running"}'
            yield "{bad"
            raise RuntimeError("stream down")

    monkeypatch.setattr(state, "client", FakeClient())

    events, error = jobs._stream_job_events("job-1", limit=10)

    assert events[0]["type"] == "job_running"
    assert events[1]["type"] == "unparseable_event"
    assert error == "stream down"


def test_extract_nested_string_and_agent_summaries():
    nested = {"payload": [{"meta": {"run_id": "run-1"}}]}
    assert jobs._extract_nested_string(nested, "run_id") == "run-1"
    assert jobs._extract_nested_string("not-nested", "run_id") == ""

    assert jobs._agent_summaries({"agents": [{"agent_id": "a"}]}, []) == [{"agent_id": "a"}]
    assert jobs._agent_summaries({}, [{"agent_id": "a", "status": "running"}, {"node_id": "b"}]) == [
        {"agent_id": "a", "status": "running", "event_count": 1},
        {"agent_id": "b", "status": "observed", "event_count": 1},
    ]


def test_find_run_dir_for_job_matches_blueprint_run_identity(monkeypatch, tmp_path):
    blueprint_run_id = "blueprint-run-1"
    record = {
        "job_id": "job-1",
        "run_id": "runtime-run-1",
        "blueprint_run_id": blueprint_run_id,
        "job": {"runtime_run_id": "nested-runtime-run-1"},
    }
    run_dir = tmp_path / blueprint_run_id
    run_dir.mkdir()
    (run_dir / "job.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(jobs, "_runs_root", lambda: tmp_path)

    for identifier in ("job-1", "runtime-run-1", blueprint_run_id, "nested-runtime-run-1"):
        assert jobs._job_record_matches(record, identifier)
    found_dir, found_record = jobs._find_run_dir_for_job(blueprint_run_id)
    assert found_dir == run_dir
    assert found_record == record
    assert not jobs._job_record_matches(record, "another-run")


def test_run_artifacts_dedupes_output_refs(monkeypatch, tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    output = tmp_path / "output.json"
    output.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("mn_api.routes.jobs.list_artifact_files", lambda run_dir: [artifact])
    monkeypatch.setattr(
        "mn_api.routes.jobs.artifact_ref",
        lambda run_id, path, run_dir: {"path": str(path), "artifact_id": "artifact"},
    )
    monkeypatch.setattr(
        "mn_api.routes.jobs.output_refs",
        lambda run_id, run_dir: [{"path": str(artifact)}, {"path": str(output), "artifact_id": "output"}],
    )

    assert jobs._run_artifacts("run-1", tmp_path) == [
        {"path": str(artifact), "artifact_id": "artifact"},
        {"path": str(output), "artifact_id": "output"},
    ]
    assert jobs._run_artifacts(None, tmp_path) == []
