import json
from datetime import datetime, timezone

from mn_api.routes import jobs


def test_compact_monitor_reads_replicated_measurements_from_its_resolved_run(monkeypatch, tmp_path):
    local_root = tmp_path / "local-runs"
    shared_run = tmp_path / "shared/submissions/definition/outputs/runs/physical-run"
    shared_run.mkdir(parents=True)
    local_root.mkdir()
    (shared_run / "run.json").write_text(json.dumps({"run_id": "physical-run", "status": "completed"}))
    (shared_run / "model_usage.jsonl").write_text(json.dumps({
        "type": "llm_usage", "event_id": "measured-call", "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"input_tokens": 16, "output_tokens": 4, "total_tokens": 20, "estimated": False},
    }) + "\n")
    monkeypatch.setattr(jobs, "_runs_root", lambda: local_root)
    monkeypatch.setattr(jobs, "_stream_job_events", lambda _: ([{"run_id": "physical-run"}], None))
    monkeypatch.setattr(jobs, "_run_dir_from_id", lambda _: shared_run)
    result = jobs._compact_job_detail("public-execution")
    usage = result["resource_usage"]
    assert usage["llm"] == {"input_tokens": 16, "output_tokens": 4, "total_tokens": 20,
                            "calls": 1, "estimated_tokens": 0}
    assert usage["model_measurements"]["source"] == "model_usage_ledger"
    assert usage["model_measurements"]["ledger_complete"] is True
    assert result["summary"]["resource_usage"]["llm"] == usage["llm"]


def test_unresolved_or_mismatched_monitor_directory_is_not_a_measured_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "read_run_resources", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Unbound read")))
    assert jobs._read_run_resource_usage("physical-run", None) is None
    assert jobs._read_run_resource_usage("physical-run", tmp_path / "another-run") is None
    assert jobs._read_run_resource_usage(None, tmp_path / "physical-run") is None


def test_core_storage_binding_precedes_local_shadow_and_missing_binding_never_reads_it(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    run = shared / "submissions/definition/outputs/runs/physical-run"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "physical-run", "status": "completed"}))
    (run / "model_usage.jsonl").write_text(json.dumps({
        "type": "llm_usage", "event_id": "measured-call", "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"input_tokens": 16, "output_tokens": 4, "total_tokens": 20, "estimated": False},
    }) + "\n")
    monkeypatch.setenv("MN_SHARED_STORAGE_ROOT", str(shared))
    monkeypatch.setattr(jobs, "_stream_job_events", lambda _: ([{"run_id": "physical-run"}], None))
    monkeypatch.setattr(jobs, "_run_dir_from_id", lambda _: (_ for _ in ()).throw(AssertionError("Local shadow read")))
    monkeypatch.setattr(jobs, "_find_run_dir_for_job", lambda _: (_ for _ in ()).throw(AssertionError("Local mapping read")))
    reference = {"storage": "syncthing", "submission_id": "definition", "run_id": "physical-run"}
    result = jobs._compact_job_detail("public-execution", run_data_ref=reference)
    assert result["resource_usage"]["llm"]["total_tokens"] == 20
    missing = jobs._compact_job_detail("public-execution", run_data_ref={**reference, "submission_id": "missing"})
    assert missing["resource_usage"] is None
