"""Launch-progress JSONL storage and projections, independent of orchestration.

The retained launch module re-exports these helpers. Keep the v2 event format,
phase ordering, terminal-state selection, and best-effort I/O behavior stable.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from mn_api import state
from mn_api.config import config_value
from mn_api.path_utils import resolve_mn_home

PROGRESS_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,220}$")
TERMINAL_LAUNCH_PROGRESS_STATUSES = {"completed", "failed"}


def launch_progress_snapshot(progress_id: str) -> dict[str, Any]:
    resolved_progress_id = validate_progress_id(progress_id)
    events = read_launch_progress(resolved_progress_id)
    latest = events[-1] if events else None
    terminal = latest_terminal_launch_event(events)
    ids = launch_progress_identifiers(events)
    error = launch_progress_error(events)
    completed = any(
        event.get("phase") == "launch" and str(event.get("status") or "").lower() in TERMINAL_LAUNCH_PROGRESS_STATUSES
        for event in events
    )
    response = {
        "version": 2,
        "progress_id": resolved_progress_id,
        "schema_version": "mn.launch_progress.v2",
        "run_id": ids.get("run_id"),
        "job_id": ids.get("job_id"),
        "events": events,
        "phases": summarize_launch_progress_phases(events),
        "latest": latest,
        "completed": completed,
        "status": (
            str(terminal.get("status") or "pending")
            if isinstance(terminal, dict)
            else str(latest.get("status") or "pending")
            if isinstance(latest, dict)
            else "pending"
        ),
        "current_phase": str(latest.get("phase") or latest.get("step") or "") if isinstance(latest, dict) else None,
    }
    if error:
        response["error"] = error
    return response


def launch_progress_has_terminal_event(progress_id: str | None) -> bool:
    return latest_terminal_launch_event(read_launch_progress(progress_id)) is not None


def latest_terminal_launch_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("phase") != "launch":
            continue
        if str(event.get("status") or "").lower() in TERMINAL_LAUNCH_PROGRESS_STATUSES:
            return event
    return None


def launch_progress_identifiers(events: list[dict[str, Any]]) -> dict[str, str | None]:
    ids: dict[str, str | None] = {"run_id": None, "job_id": None}
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        for key in ("run_id", "job_id"):
            value = details.get(key)
            if value is not None:
                ids[key] = str(value)
    return ids


def launch_progress_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if str(event.get("status") or "").lower() != "failed":
            continue
        message = str(event.get("message") or "").strip()
        if message:
            return message
        detail = str(event.get("detail") or "").strip()
        if detail:
            return detail
    return None


def validate_progress_id(progress_id: str | None) -> str | None:
    if progress_id is None:
        return None
    value = str(progress_id).strip()
    if not value:
        return None
    if not PROGRESS_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="invalid progress id")
    return value


def launch_progress_root() -> Path:
    configured = config_value("MN_LAUNCH_PROGRESS_DIR")
    if configured:
        return Path(configured).expanduser()
    return resolve_mn_home() / "launch_progress"


def launch_progress_path(progress_id: str) -> Path:
    return launch_progress_root() / f"{progress_id}.jsonl"


def record_launch_progress(
    progress_id: str | None,
    phase: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    detail: str | None = None,
    expectation: str | None = None,
    severity: str | None = None,
) -> None:
    if not progress_id:
        return
    event: dict[str, Any] = {
        "version": 2,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": phase,
        "status": status,
        "message": message,
    }
    if label:
        event["label"] = label
    if detail:
        event["detail"] = detail
    if expectation:
        event["expectation"] = expectation
    if severity:
        event["severity"] = severity
    if details is not None:
        event["details"] = details
    try:
        path = launch_progress_path(progress_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str, separators=(",", ":")) + "\n")
    except OSError:
        state.logger.exception("Failed to record blueprint launch progress")


def read_launch_progress(progress_id: str | None) -> list[dict[str, Any]]:
    if not progress_id:
        return []
    path = launch_progress_path(progress_id)
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        state.logger.exception("Failed to read blueprint launch progress")
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def launch_progress_phase_label(phase: str) -> str:
    labels = {
        "resolve_source": "Find blueprint",
        "requirements": "Check runtime resources",
        "model_install": "Prepare runtime models",
        "context_engine": "Prepare context memory",
        "validation": "Validate inputs",
        "cleanup": "Clean stale runs",
        "pre_launch": "Start local helpers",
        "prepare_bundle": "Package workflow",
        "submit": "Submit runtime job",
        "launch": "Launch",
    }
    return labels.get(phase, phase.replace("_", " ").title())


def summarize_launch_progress_phases(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
    for event in events:
        phase = str(event.get("phase") or event.get("step") or event.get("id") or "").strip()
        if not phase:
            continue
        phase_record = {
            "version": 2,
            "id": phase,
            "label": str(event.get("label") or launch_progress_phase_label(phase)),
            "status": str(event.get("status") or event.get("state") or "running"),
            "message": str(event.get("message") or event.get("detail") or ""),
            "updated_at": str(event.get("ts") or event.get("timestamp") or ""),
        }
        for key in ("detail", "expectation", "severity", "details"):
            if key in event:
                phase_record[key] = event[key]
        phases[phase] = phase_record
    return list(phases.values())
