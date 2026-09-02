from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.request import Request

import pytest
from fastapi import HTTPException

from mn_api import job_store, state
from mn_api.routes import jobs


def test_shared_job_ui_dir_uses_runtime_shared_storage(monkeypatch, tmp_path):
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(
        job_store.RuntimeConfig,
        "from_env",
        lambda: SimpleNamespace(shared_storage_root=str(shared_root)),
    )

    expected = shared_root / "job-ui" / "job-1"
    assert job_store.shared_job_ui_dir_from_id("job-1", must_exist=False) == expected
    assert job_store.shared_job_ui_dir_from_id("../job-1", must_exist=False) is None
    assert job_store.shared_job_ui_dir_from_id("job-1") is None
    expected.mkdir(parents=True)
    assert job_store.shared_job_ui_dir_from_id("job-1") == expected


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


def test_get_job_ui_reads_the_durable_job_data_directory(monkeypatch, tmp_path):
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()
    (job_dir / "ui.json").write_text(json.dumps({"job_id": "job-1", "title": "Example"}), encoding="utf-8")
    (job_dir / "web_ui.json").write_text(
        json.dumps({"job_id": "job-1", "url": "http://127.0.0.1:61000"}), encoding="utf-8"
    )
    monkeypatch.setattr(jobs, "job_data_dir_from_id", lambda _job_id, must_exist=False: job_dir)
    monkeypatch.setattr(
        jobs,
        "shared_job_ui_dir_from_id",
        lambda _job_id, must_exist=False: tmp_path / "missing-shared",
    )

    assert jobs.get_job_ui("job-1") == {
        "job_id": "job-1",
        "ui": {"job_id": "job-1", "title": "Example"},
        "web_ui": {"job_id": "job-1", "url": "http://127.0.0.1:61000"},
    }


def test_get_job_ui_prefers_the_cross_node_shared_handle(monkeypatch, tmp_path):
    local_dir = tmp_path / "local" / "job-1"
    shared_dir = tmp_path / "shared" / "job-1"
    local_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)
    for directory, port in ((local_dir, 61000), (shared_dir, 44161)):
        (directory / "ui.json").write_text(
            json.dumps({"job_id": "job-1", "title": "Example"}),
            encoding="utf-8",
        )
        (directory / "web_ui.json").write_text(
            json.dumps({"job_id": "job-1", "url": f"http://10.0.4.26:{port}"}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        jobs, "job_data_dir_from_id", lambda _job_id, must_exist=False: local_dir
    )
    monkeypatch.setattr(
        jobs,
        "shared_job_ui_dir_from_id",
        lambda _job_id, must_exist=False: shared_dir,
    )

    assert jobs.get_job_ui("job-1")["web_ui"]["url"] == "http://10.0.4.26:44161"


def test_get_job_ui_rejects_handles_owned_by_another_job(monkeypatch, tmp_path):
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()
    (job_dir / "ui.json").write_text(json.dumps({"job_id": "job-2"}), encoding="utf-8")
    (job_dir / "web_ui.json").write_text(json.dumps({"job_id": "job-2"}), encoding="utf-8")
    monkeypatch.setattr(jobs, "job_data_dir_from_id", lambda _job_id, must_exist=False: job_dir)
    monkeypatch.setattr(
        jobs,
        "shared_job_ui_dir_from_id",
        lambda _job_id, must_exist=False: tmp_path / "missing-shared",
    )

    with pytest.raises(HTTPException) as error:
        jobs.get_job_ui("job-1")
    assert error.value.status_code == 404


def test_get_job_ui_falls_back_to_its_federated_owner(monkeypatch, tmp_path):
    remote_handle = {
        "job_id": "job-1",
        "ui": {"job_id": "job-1", "title": "Example"},
        "web_ui": {"job_id": "job-1", "url": "http://10.0.4.26:45767"},
    }

    class FakeClient:
        def get_job(self, _job_id):
            return json.dumps({"owner_node": "mirror_neuron@10.0.4.26"})

        def get_system_summary(self):
            return json.dumps(
                {
                    "nodes": [
                        {
                            "name": "mirror_neuron@10.0.4.26",
                            "address": "10.0.4.26",
                            "self?": False,
                        }
                    ]
                }
            )

    class FakeResponse:
        def read(self):
            return json.dumps(remote_handle).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    seen: list[Request] = []
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(
        state,
        "refresh_config_from_env",
        lambda: SimpleNamespace(port=54001, api_token=""),
    )
    monkeypatch.setattr(jobs, "job_data_dir_from_id", lambda *_args, **_kwargs: tmp_path / "missing")
    monkeypatch.setattr(jobs, "shared_job_ui_dir_from_id", lambda *_args, **_kwargs: tmp_path / "also-missing")
    monkeypatch.setattr(
        jobs.urllib.request,
        "urlopen",
        lambda request, timeout: seen.append(request) or FakeResponse(),
    )

    assert jobs.get_job_ui("job-1") == remote_handle
    assert seen[0].full_url == "http://10.0.4.26:54001/api/v1/jobs/job-1/ui"


def test_get_job_ui_never_uses_an_untrusted_owner_address(monkeypatch, tmp_path):
    class FakeClient:
        def get_job(self, _job_id):
            return json.dumps({"owner_node": "mirror_neuron@bad"})

        def get_system_summary(self):
            return json.dumps(
                {
                    "nodes": [
                        {
                            "name": "mirror_neuron@bad",
                            "address": "http://untrusted.example/path",
                            "self?": False,
                        }
                    ]
                }
            )

    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(jobs, "job_data_dir_from_id", lambda *_args, **_kwargs: tmp_path / "missing")
    monkeypatch.setattr(jobs, "shared_job_ui_dir_from_id", lambda *_args, **_kwargs: tmp_path / "also-missing")
    monkeypatch.setattr(
        jobs.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("invalid owner host must not be requested"),
    )

    with pytest.raises(HTTPException) as error:
        jobs.get_job_ui("job-1")
    assert error.value.status_code == 404
