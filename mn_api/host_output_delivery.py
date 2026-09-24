"""Reconcile submitting-host delivery for runs started outside the API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re

from mn_sdk.blueprint_support.shared_outputs import load_submission_storage

from mn_api import state
from mn_api.blueprints import shared_runs_root, start_background_run_relay


_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LOG = logging.getLogger(__name__)


def _relay_running(run_dir: Path) -> bool:
    try:
        metadata = json.loads((run_dir / "event_relay.json").read_text(encoding="utf-8"))
        pid = int(metadata.get("pid") or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def reconcile_host_output_delivery() -> int:
    """Start missing host relays after a completed Core run receipt replicates."""
    config = state.refresh_config_from_env()
    shared_root = Path(config.shared_storage_root).expanduser().resolve()
    submissions = shared_root / "submissions"
    runs_root = Path(shared_runs_root()).expanduser()
    started = 0
    if not submissions.is_dir():
        return started
    for submission in submissions.iterdir():
        if not submission.is_dir() or submission.is_symlink():
            continue
        storage = load_submission_storage(shared_root, submission.name)
        if not storage.get("output_copy"):
            continue
        run_outputs = submission / "outputs" / "runs"
        if not run_outputs.is_dir():
            continue
        for run in run_outputs.iterdir():
            if not run.is_dir() or run.is_symlink() or not _RUN_ID.fullmatch(run.name):
                continue
            try:
                completion = json.loads((run / ".mn_completion.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(completion, dict)
                or completion.get("status") != "completed"
                or completion.get("run_id") != run.name
            ):
                continue
            local_run = runs_root / run.name
            if (local_run / "shared_output_materialized.json").is_file() or _relay_running(local_run):
                continue
            start_background_run_relay(run.name, run.name, storage)
            started += 1
    return started


async def host_output_delivery_loop(interval_seconds: float = 10.0) -> None:
    while True:
        try:
            await asyncio.to_thread(reconcile_host_output_delivery)
        except Exception:
            _LOG.exception("host output delivery reconciliation failed")
        await asyncio.sleep(interval_seconds)
