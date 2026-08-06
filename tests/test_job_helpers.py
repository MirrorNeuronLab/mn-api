from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mn_api import state
from mn_api.routes import jobs


def test_decode_manifest_and_submission_run_id(monkeypatch):
    assert jobs._decode_manifest('{"graph_id":"g"}') == {"graph_id": "g"}
    assert jobs._submission_run_id('{"metadata":{"mn_cli":{"run_id":" run-1 "}}}') == "run-1"
    assert jobs._submission_run_id("{bad") is None

    with pytest.raises(HTTPException):
        jobs._decode_manifest("{bad")
    with pytest.raises(HTTPException):
        jobs._decode_manifest("[]")

    monkeypatch.setattr("mn_api.routes.jobs.is_manifest_source", lambda manifest: True)
    monkeypatch.setattr("mn_api.routes.jobs.expand_manifest_source", lambda manifest, root_dir=None: {"expanded": root_dir})
    assert jobs._decode_manifest('{"source":"x"}', root_dir="root") == {"expanded": "root"}


def test_validate_job_manifest_reports_spec_hardware_and_input_failures(monkeypatch):
    monkeypatch.setattr("mn_api.routes.jobs.validate_resource_spec_issues", lambda manifest: [])
    monkeypatch.setattr("mn_api.routes.jobs.validate_input_validation_spec_issues", lambda manifest: [])
    monkeypatch.setattr("mn_api.routes.jobs.make_validation_report", lambda issues: {"ok": False, "issues": issues})

    monkeypatch.setattr("mn_api.routes.jobs.validate_requirements_spec_issues", lambda manifest: [{"code": "bad"}])
    spec_response = jobs._validate_job_manifest('{"graph_id":"g"}', force=False)
    assert spec_response.status_code == 422

    monkeypatch.setattr("mn_api.routes.jobs.validate_requirements_spec_issues", lambda manifest: [])
    monkeypatch.setattr("mn_api.routes.jobs.run_hardware_requirements_validation", lambda manifest, resource_report=None, force=False: {"ok": False})
    hardware_response = jobs._validate_job_manifest('{"graph_id":"g"}', force=False)
    assert hardware_response.status_code == 412

    monkeypatch.setattr("mn_api.routes.jobs.run_hardware_requirements_validation", lambda manifest, resource_report=None, force=False: {"ok": True})
    monkeypatch.setattr("mn_api.routes.jobs.run_input_validation", lambda path, manifest: {"ok": False})
    input_response = jobs._validate_job_manifest('{"graph_id":"g"}', force=False)
    assert input_response.status_code == 422
    assert jobs._validate_job_manifest('{"graph_id":"g"}', force=True) is None


def test_validate_job_bundle_uses_bundle_root_for_input_validation(monkeypatch, tmp_path):
    observed = {}
    monkeypatch.setattr("mn_api.routes.jobs.validate_requirements_spec_issues", lambda manifest: [])
    monkeypatch.setattr("mn_api.routes.jobs.validate_resource_spec_issues", lambda manifest: [])
    monkeypatch.setattr("mn_api.routes.jobs.validate_input_validation_spec_issues", lambda manifest: [])
    monkeypatch.setattr("mn_api.routes.jobs.run_hardware_requirements_validation", lambda manifest, resource_report=None, force=False: {"ok": True})

    def fake_input_validation(path, manifest):
        observed["path"] = path
        return {"ok": True}

    monkeypatch.setattr("mn_api.routes.jobs.run_input_validation", fake_input_validation)

    assert jobs._validate_job_bundle(str(tmp_path), '{"graph_id":"g"}', force=False) is None
    assert observed["path"] == tmp_path


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


def test_legacy_job_control_error_requires_callable_details():
    assert jobs._legacy_job_control_error(RuntimeError("boom")) is None
    assert jobs._legacy_job_control_error(SimpleNamespace(details=lambda: "")) is None
    response = jobs._legacy_job_control_error(SimpleNamespace(details=lambda: "job cannot pause"))
    assert response.status_code == 500
    assert json.loads(response.body) == {"version": 2, "error": "job cannot pause"}
