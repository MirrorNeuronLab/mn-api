from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any
import uuid

from fastapi import HTTPException

from mn_api import state
from mn_api.errors import http_exception_problem, public_validation_issues
from mn_api.public import decode, first_identifier, public_value
from mn_api import operation_journal
from mn_api.path_utils import resolve_mn_home
from mn_sdk.errors import AppError, normalize_exception
from mn_sdk.openshell_errors import public_details as openshell_error_details


_MAX_OPERATIONS = 10_000
_operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
_lock = threading.Lock()
_condition = threading.Condition(_lock)
_local_operation_ids: set[str] = set()
_local_operation_events: dict[str, list[dict[str, Any]]] = {}
_durable_local_operation_ids: set[str] = set()
_active_local_operation_ids: set[str] = set()


def _durable_operation_path(operation_id: str) -> Path:
    if not operation_id.startswith("op-local-") or not operation_id[9:].isalnum():
        raise ValueError("Invalid local operation ID.")
    return resolve_mn_home() / "api_operations" / f"{operation_id}.json"


def _load_durable_operation_locked(operation_id: str) -> dict[str, Any] | None:
    cached = _operations.get(operation_id)
    if cached is not None and operation_id not in _durable_local_operation_ids:
        return cached
    try:
        path = _durable_operation_path(operation_id)
    except ValueError:
        return None
    record = operation_journal.read(path, operation_id)
    if record is None:
        if operation_id in _durable_local_operation_ids:
            raise HTTPException(503, "The accepted operation receipt is unavailable.")
        return None
    operation, events = record
    if operation.get("status") in {"pending", "running"} and operation_id not in _active_local_operation_ids:
        operation = dict(operation)
        operation["status"] = "interrupted"
        previous_progress = operation.get("progress") or {}
        operation["progress"] = {
            "percent": previous_progress.get("percent", 0),
            "stage": "reconcile",
            "label": "Reconnect work",
            "detail": "The API restarted. Resubmit the same request to reconcile its result.",
        }
        events = _next_events(events, "operation.interrupted", operation)
        operation_journal.write(path, operation, events)
        state.logger.info("Operation receipt recovered operation_id=%s status=interrupted", operation_id)
    _local_operation_ids.add(operation_id)
    _durable_local_operation_ids.add(operation_id)
    _local_operation_events[operation_id] = events
    return _store_operation_locked(operation)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _operation_title(code: str) -> str:
    return str(code or "operation_failed").replace("MN_", "").replace("_", " ").strip().title()


def _operation_error(error: Exception) -> dict[str, Any]:
    if isinstance(error, HTTPException):
        return http_exception_problem(error)
    app_error = error if isinstance(error, AppError) else normalize_exception(error)
    result = {
        "code": app_error.code,
        "title": _operation_title(app_error.code),
        "detail": app_error.user_message,
        "status": app_error.http_status,
        "retryable": app_error.http_status >= 500,
    }
    if app_error.hint:
        result["hint"] = app_error.hint
    preparation = openshell_error_details(app_error)
    if preparation:
        result["details"] = preparation
        result["problem_code"] = app_error.problem_code
        result["category"] = app_error.category
    if app_error.details and "blockers" in app_error.details:
        from mn_sdk.errors import sanitize_context

        result["details"] = {"blockers": sanitize_context(app_error.details)["blockers"]}
        result["problem_code"] = app_error.problem_code
        result["category"] = app_error.category
    issues = getattr(error, "operation_issues", None)
    if isinstance(issues, list) and issues:
        result["errors"] = public_validation_issues({"issues": issues})
    return result


def _next_events(events: list[dict], event_type: str, resource: dict) -> list[dict]:
    sequence = int(events[-1]["sequence"]) + 1 if events else 1
    return [*events[-199:], {"sequence": sequence, "type": event_type, "data": public_value(resource)}]


def _publish_locked(resource: dict, event_type: str, *, durable: bool) -> dict:
    operation_id = resource["operation_id"]
    events = _next_events(_local_operation_events.get(operation_id, []), event_type, resource)
    if durable:
        operation_journal.write(_durable_operation_path(operation_id), resource, events)
        _durable_local_operation_ids.add(operation_id)
    _local_operation_ids.add(operation_id)
    _local_operation_events[operation_id] = events
    stored = _store_operation_locked(resource)
    _condition.notify_all()
    return stored


def _store_operation_locked(resource: dict[str, Any]) -> dict[str, Any]:
    operation_id = str(resource["operation_id"])
    _operations[operation_id] = resource
    _operations.move_to_end(operation_id)
    _trim_cache_locked()
    return dict(resource)


def _trim_cache_locked():
    while len(_operations) - len(_active_local_operation_ids) > _MAX_OPERATIONS:
        removed_id = next((key for key in _operations if key not in _active_local_operation_ids), None)
        if removed_id is None:
            break
        del _operations[removed_id]
        _durable_local_operation_ids.discard(removed_id)
        _local_operation_ids.discard(removed_id)
        _local_operation_events.pop(removed_id, None)


def register_operation(value: Any) -> dict[str, Any]:
    resource = public_value(decode(value))
    if not isinstance(resource, dict):
        resource = {"status": "accepted"}
    operation_id = first_identifier(resource, ("operation_id", "id"))
    if operation_id:
        resource.setdefault("operation_id", operation_id)
        with _lock:
            _store_operation_locked(resource)
    return resource


def start_operation(kind: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    return register_operation(state.client.start_operation(kind, options or {}))


def complete_local_operation(
    kind: str,
    options: dict[str, Any] | None = None,
    *,
    result: Any = None,
) -> dict[str, Any]:
    """Register an API-owned operation that completed before the response.

    Blueprint catalog and addition work is owned by mn-api because it uses
    the API host's configured catalog and model registry. Core must not be
    asked to execute those filesystem-scoped operations.
    """
    now = _now()
    operation_id = f"op-local-{uuid.uuid4().hex}"
    operation = {
        "operation_id": operation_id,
        "kind": kind,
        "status": "completed",
        "created_at": now,
        "updated_at": now,
        "options": public_value(options or {}),
        "progress": {
            "percent": 100,
            "stage": "completed",
            "label": "Completed",
            "detail": "The operation completed successfully.",
        },
        "result": public_value(result),
    }
    with _condition:
        return _publish_locked(operation, "operation.completed", durable=False)


def update_local_operation(
    operation_id: str,
    *,
    status: str | None = None,
    percent: int | float | None = None,
    stage: str | None = None,
    label: str | None = None,
    detail: str | None = None,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _condition:
        current = _operations.get(operation_id)
        if current is None or operation_id not in _local_operation_ids:
            raise KeyError(operation_id)
        updated = dict(current)
        if status:
            updated["status"] = status
        if percent is not None or stage or label or detail:
            progress = dict(updated.get("progress") or {})
            if percent is not None:
                previous_percent = float(progress.get("percent") or 0)
                progress["percent"] = max(round(previous_percent), max(0, min(100, round(float(percent)))))
            if stage:
                progress["stage"] = stage
            if label:
                progress["label"] = label
            if detail:
                progress["detail"] = detail
            updated["progress"] = progress
        if result is not None:
            updated["result"] = public_value(result)
        if error is not None:
            updated["error"] = public_value(error)
        updated["updated_at"] = _now()
        event_type = (
            "operation.completed"
            if updated.get("status") == "completed"
            else "operation.failed"
            if updated.get("status") == "failed"
            else "operation.progress"
        )
        return _publish_locked(updated, event_type, durable=operation_id in _durable_local_operation_ids)


def start_local_operation(
    kind: str,
    options: dict[str, Any],
    work,
    *,
    operation_id: str | None = None,
    request_fingerprint: str | None = None,
    durable: bool = False,
    retry_failed: bool = False,
) -> dict[str, Any]:
    now = _now()
    operation_id = operation_id or f"op-local-{uuid.uuid4().hex}"
    operation = {
        "operation_id": operation_id,
        "kind": kind,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "options": public_value(options),
        **({"request_fingerprint": request_fingerprint} if request_fingerprint else {}),
        "progress": {
            "percent": 0,
            "stage": "queued",
            "label": "Queued",
            "detail": "The operation is queued on the local API host.",
        },
    }
    with _condition:
        existing = _load_durable_operation_locked(operation_id) if durable else _operations.get(operation_id)
        if existing:
            if request_fingerprint and existing.get("request_fingerprint") != request_fingerprint:
                raise HTTPException(
                    status_code=409, detail="The Idempotency-Key was already used with another request."
                )
            if (
                existing.get("status") == "completed"
                or (
                    existing.get("status") == "failed"
                    and not (existing.get("error") or {}).get("retryable")
                    and not retry_failed
                )
                or operation_id in _active_local_operation_ids
            ):
                return dict(existing)
            operation["created_at"] = existing.get("created_at", now)
            operation["options"] = existing.get("options", operation["options"])
        # The condition serializes matching submissions. Pin during insertion,
        # but never let an unsuccessful write leave a published or active claim.
        _active_local_operation_ids.add(operation_id)
        try:
            stored = _publish_locked(operation, "operation.accepted", durable=durable)
        except BaseException:
            _active_local_operation_ids.discard(operation_id)
            raise

    def report_progress(**progress):
        update_local_operation(operation_id, status="running", **progress)

    def run():
        try:
            update_local_operation(
                operation_id,
                status="running",
                percent=1,
                stage="starting",
                label="Starting",
                detail="The local API host started the operation.",
            )
            try:
                result = work(report_progress)
            except Exception as exc:
                update_local_operation(operation_id, status="failed", error=_operation_error(exc))
            else:
                update_local_operation(
                    operation_id,
                    status="completed",
                    percent=100,
                    stage="completed",
                    label="Completed",
                    detail="The operation completed successfully.",
                    result=result,
                )
        except Exception:
            state.logger.error("Local operation publication failed operation_id=%s kind=%s", operation_id, kind)
        finally:
            with _condition:
                _active_local_operation_ids.discard(operation_id)
                _trim_cache_locked()
                _condition.notify_all()

    try:
        threading.Thread(target=run, name=f"mn-api-{kind}-{operation_id[-8:]}", daemon=True).start()
    except RuntimeError as exc:
        with _condition:
            _active_local_operation_ids.discard(operation_id)
            _condition.notify_all()
        raise HTTPException(503, "The local operation worker could not start.") from exc
    return stored


def is_local_operation(operation_id: str) -> bool:
    with _lock:
        _load_durable_operation_locked(operation_id)
        return operation_id in _local_operation_ids


def stream_local_operation_events(operation_id: str, *, resume_after: int = 0):
    next_sequence = max(0, resume_after) + 1
    while True:
        with _condition:
            while True:
                operation = _load_durable_operation_locked(operation_id)
                events = [
                    dict(event)
                    for event in _local_operation_events.get(operation_id, [])
                    if int(event.get("sequence") or 0) >= next_sequence
                ]
                terminal = str((operation or {}).get("status") or "").lower() in {
                    "completed",
                    "failed",
                    "cancelled",
                    "canceled",
                    "interrupted",
                }
                if events or terminal or operation is None:
                    break
                _condition.wait(timeout=15)
            if operation is None:
                return
        if not events:
            return
        for event in events:
            next_sequence = int(event["sequence"]) + 1
            yield event
        if terminal:
            return


def get_operation(operation_id: str) -> dict[str, Any]:
    with _lock:
        local = _load_durable_operation_locked(operation_id)
    if operation_id.startswith("op-local-") and local is not None:
        return dict(local)
    if operation_id.startswith("op-local-"):
        raise HTTPException(status_code=404, detail="Operation not found.")
    resource = register_operation(state.client.get_operation(operation_id))
    if not resource.get("operation_id"):
        resource["operation_id"] = operation_id
    return resource


def known_operations() -> list[dict[str, Any]]:
    with _lock:
        return [dict(operation) for operation in _operations.values()]


def sse_envelope(*, event_id: int, event_type: str, resource: str, data: Any) -> dict[str, Any]:
    return {
        "id": str(event_id),
        "type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "resource": resource,
        "data": public_value(decode(data)),
    }


def encode_sse(envelope: dict[str, Any]) -> str:
    return f"id: {envelope['id']}\nevent: {envelope['type']}\ndata: {json.dumps(envelope, separators=(',', ':'))}\n\n"
