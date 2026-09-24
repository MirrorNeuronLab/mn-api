import json

from mn_api import run_recovery
from mn_api.routes.v1 import jobs


def test_recover_stored_workflow_progress(tmp_path, monkeypatch):
    local = tmp_path / "runs" / "bootstrap"
    local.mkdir(parents=True)
    (local / "job.json").write_text(json.dumps({"run_id": "remote-1"}), encoding="utf-8")
    (local / "manifest.json").write_text(json.dumps({
        "workflow": {"steps": [{"id": "prepare", "run": "prepare"}]},
    }), encoding="utf-8")
    remote = tmp_path / "shared" / "remote-1"
    remote.mkdir(parents=True)
    (remote / "run.json").write_text(json.dumps({"run_id": "remote-1", "status": "failed"}), encoding="utf-8")
    (remote / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(run_recovery, "shared_run_dir", lambda _run_id: remote)
    monkeypatch.setattr(run_recovery, "runs_root", lambda: local.parent)

    assert run_recovery.stored_run("remote-1")["status"] == "failed"
    progress = run_recovery.stored_progress("remote-1")
    assert progress["status"] == "failed"
    assert [step["id"] for step in progress["steps"]] == ["prepare"]


def test_stale_running_shared_record_is_unknown(tmp_path, monkeypatch):
    remote = tmp_path / "remote-1"
    remote.mkdir()
    (remote / "run.json").write_text(json.dumps({"run_id": "remote-1", "status": "running"}), encoding="utf-8")
    monkeypatch.setattr(run_recovery, "shared_run_dir", lambda _run_id: remote)

    assert run_recovery.stored_run("remote-1")["status"] == "unknown"


def test_api_run_list_keeps_mapped_runs_and_prefers_core_status(monkeypatch):
    monkeypatch.setattr(jobs, "list_local_runs", lambda: [
        {"run_id": "old", "status": "failed"},
        {"run_id": "stale", "status": "running"},
        {"run_id": "live", "status": "unknown"},
    ])

    class Service:
        def list_jobs(self, **_kwargs):
            return {"items": [{"job_id": "job-1"}]}

        def list_runs(self, _job_id):
            return {"items": [{"run_id": "live", "status": "running"}]}

    monkeypatch.setattr(jobs, "_service", lambda: Service())

    result = {record["run_id"]: record for record in jobs._all_runs()}

    assert result["old"]["status"] == "failed"
    assert result["stale"]["status"] == "unknown"
    assert result["live"]["status"] == "running"
