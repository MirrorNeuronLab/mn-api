"""API-only status reporting around synchronous launch work."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Thread

ProgressCallback = Callable[[str, str, str], None]
HEARTBEAT_SECONDS = 10.0


def progress_reporter(progress_id: str | None, phase: str = "prepare_bundle") -> ProgressCallback | None:
    if not progress_id:
        return None

    def report(message: str, detail: str, expectation: str) -> None:
        from mn_api.routes.blueprints import record_launch_progress

        record_launch_progress(progress_id, phase, "running", message, detail=detail, expectation=expectation)

    return report


@contextmanager
def observe_submission(progress_id: str | None) -> Iterator[None]:
    """Record request lifecycle only; job ownership and error handling stay with the caller."""
    from mn_api.routes.blueprints import record_launch_progress

    record_launch_progress(progress_id, "launch", "running", "Submission request started.")
    try:
        yield
    except BaseException:
        record_launch_progress(
            progress_id,
            "launch",
            "failed",
            "Submission request stopped. Check its response and job status before retrying.",
        )
        raise
    else:
        record_launch_progress(progress_id, "launch", "completed", "Submission request completed.")


def public_progress_snapshot(progress_id: str) -> dict:
    """Expose presentation fields, excluding configs, validation payloads, and diagnostics."""
    from mn_api.routes.blueprints import launch_progress_snapshot

    snapshot = launch_progress_snapshot(progress_id)

    def event(value: dict | None) -> dict | None:
        if value is None:
            return None
        result = {
            key: value[key]
            for key in ("id", "phase", "status", "label", "message", "detail", "expectation", "ts", "updated_at")
            if key in value
        }
        if value.get("status") in {"failed", "error", "cancelled", "canceled"}:
            result["message"] = "Submission request stopped. Check its response and job status before retrying."
            result.pop("detail", None)
            result.pop("expectation", None)
        return result

    return {
        "progress_id": progress_id,
        "status": snapshot["status"],
        "completed": snapshot["completed"],
        "current_phase": snapshot["current_phase"],
        "latest": event(snapshot["latest"]),
        "phases": [event(value) for value in snapshot["phases"]],
        "events": [event(value) for value in snapshot["events"][-200:]],
    }


@contextmanager
def launch_activity(
    report: ProgressCallback | None, message: str, detail: str, expectation: str = ""
) -> Iterator[None]:
    """Keep the existing progress record current without inspecting runtime logs."""
    if report is None:
        yield
        return
    started = time.monotonic()
    stopped = Event()

    def emit(current_detail: str) -> None:
        try:
            report(message, current_detail, expectation)
        except Exception:
            # An unavailable progress sink must not change submission behavior.
            pass

    def heartbeat() -> None:
        while not stopped.wait(HEARTBEAT_SECONDS):
            elapsed = max(0, int(time.monotonic() - started))
            emit(f"{detail} Still waiting; {elapsed}s elapsed in this stage.")

    emit(detail)
    thread = Thread(target=heartbeat, name="mn-api-launch-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
