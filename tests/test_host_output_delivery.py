from __future__ import annotations

import json
import os
from types import SimpleNamespace

from mn_api import host_output_delivery


def test_reconciles_completed_scheduled_run_without_desktop_copy(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    submission = shared / "submissions" / "job-1-def-current"
    run = submission / "outputs" / "runs" / "scheduled-1"
    run.mkdir(parents=True)
    storage = {
        "host_root": str(shared),
        "host_submission_path": str(submission),
        "runtime_root": "/runtime/shared",
        "output_copy": [{
            "source_path": "/runtime/shared/submissions/job-1-def-current/outputs/user",
            "target_path": str(tmp_path / "Downloads" / "report"),
        }],
    }
    (submission / "submission.json").write_text(json.dumps(storage), encoding="utf-8")
    (run / ".mn_completion.json").write_text(
        json.dumps({"run_id": "scheduled-1", "status": "completed", "output_files": []}),
        encoding="utf-8",
    )
    local_runs = tmp_path / "runs"
    local_run = local_runs / "scheduled-1"
    local_run.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        host_output_delivery.RuntimeConfig,
        "from_env",
        lambda: SimpleNamespace(shared_storage_root=str(shared)),
    )
    monkeypatch.setattr(host_output_delivery, "shared_runs_root", lambda: str(local_runs))
    monkeypatch.setattr(
        host_output_delivery, "start_background_run_relay",
        lambda *args: calls.append(args),
    )

    assert host_output_delivery.reconcile_host_output_delivery() == 1
    assert calls == [("scheduled-1", "scheduled-1", storage)]

    (local_run / "event_relay.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    assert host_output_delivery.reconcile_host_output_delivery() == 0
    (local_run / "event_relay.json").unlink()
    (local_run / "shared_output_materialized.json").write_text('{"ok":true}', encoding="utf-8")
    assert host_output_delivery.reconcile_host_output_delivery() == 0
