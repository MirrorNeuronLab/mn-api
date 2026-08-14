from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import threading
from typing import Any
import uuid

from fastapi import HTTPException

from mn_api import state
from mn_api.public import decode, first_identifier, public_value
from mn_sdk.errors import AppError, normalize_exception


_MAX_OPERATIONS = 10_000
_operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
_lock = threading.Lock()
_condition = threading.Condition(_lock)
_local_operation_ids: set[str] = set()
_local_operation_events: dict[str, list[dict[str, Any]]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _operation_title(code: str) -> str:
    return str(code or "operation_failed").replace("MN_", "").replace("_", " ").strip().title()


def _operation_error(error: Exception) -> dict[str, Any]:
    if isinstance(error, HTTPException):
        code = "not_found" if error.status_code == 404 else "operation_failed"
        detail = (
            error.detail
            if error.status_code < 500 and isinstance(error.detail, str)
            else "The API could not complete the operation."
        )
        return {
            "code": code,
            "title": _operation_title(code),
            "detail": detail,
            "retryable": error.status_code >= 500,
        }
    app_error = error if isinstance(error, AppError) else normalize_exception(error)
    result = {
        "code": app_error.code,
        "title": _operation_title(app_error.code),
        "detail": app_error.user_message,
        "retryable": app_error.http_status >= 500,
    }
    if app_error.hint:
        result["hint"] = app_error.hint
    issues = getattr(error, "operation_issues", None)
    if isinstance(issues, list) and issues:
        result["errors"] = public_value(issues[:100])
    return result


def _append_local_event_locked(operation_id: str, event_type: str, resource: dict[str, Any]) -> None:
    events = _local_operation_events.setdefault(operation_id, [])
    sequence = int(events[-1]["sequence"]) + 1 if events else 1
    events.append(
        {
            "sequence": sequence,
            "type": event_type,
            "data": public_value(resource),
        }
    )
    if len(events) > 200:
        del events[:-200]


def _store_operation_locked(resource: dict[str, Any]) -> dict[str, Any]:
    operation_id = str(resource["operation_id"])
    _operations[operation_id] = resource
    _operations.move_to_end(operation_id)
    while len(_operations) > _MAX_OPERATIONS:
        removed_id, _removed = _operations.popitem(last=False)
        _local_operation_ids.discard(removed_id)
        _local_operation_events.pop(removed_id, None)
    return dict(resource)


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
        _local_operation_ids.add(operation_id)
        stored = _store_operation_locked(operation)
        _append_local_event_locked(operation_id, "operation.completed", stored)
        _condition.notify_all()
        return stored


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
        stored = _store_operation_locked(updated)
        event_type = (
            "operation.completed"
            if updated.get("status") == "completed"
            else "operation.failed"
            if updated.get("status") == "failed"
            else "operation.progress"
        )
        _append_local_event_locked(operation_id, event_type, stored)
        _condition.notify_all()
        return stored


def start_local_operation(kind: str, options: dict[str, Any], work) -> dict[str, Any]:
    now = _now()
    operation_id = f"op-local-{uuid.uuid4().hex}"
    operation = {
        "operation_id": operation_id,
        "kind": kind,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "options": public_value(options),
        "progress": {
            "percent": 0,
            "stage": "queued",
            "label": "Queued",
            "detail": "The operation is queued on the local API host.",
        },
    }
    with _condition:
        _local_operation_ids.add(operation_id)
        stored = _store_operation_locked(operation)
        _append_local_event_locked(operation_id, "operation.accepted", stored)
        _condition.notify_all()

    def report_progress(**progress):
        update_local_operation(operation_id, status="running", **progress)

    def run():
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
            update_local_operation(
                operation_id,
                status="failed",
                error=_operation_error(exc),
            )
            return
        update_local_operation(
            operation_id,
            status="completed",
            percent=100,
            stage="completed",
            label="Completed",
            detail="The operation completed successfully.",
            result=result,
        )

    threading.Thread(target=run, name=f"mn-api-{kind}-{operation_id[-8:]}", daemon=True).start()
    return stored


def is_local_operation(operation_id: str) -> bool:
    with _lock:
        return operation_id in _local_operation_ids


def stream_local_operation_events(operation_id: str, *, resume_after: int = 0):
    next_sequence = max(0, resume_after) + 1
    while True:
        with _condition:
            while True:
                events = [
                    dict(event)
                    for event in _local_operation_events.get(operation_id, [])
                    if int(event.get("sequence") or 0) >= next_sequence
                ]
                operation = _operations.get(operation_id)
                terminal = str((operation or {}).get("status") or "").lower() in {
                    "completed",
                    "failed",
                    "cancelled",
                    "canceled",
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
        local = _operations.get(operation_id)
    if operation_id.startswith("op-local-") and local is not None:
        return dict(local)
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
    return (
        f"id: {envelope['id']}\n"
        f"event: {envelope['type']}\n"
        f"data: {json.dumps(envelope, separators=(',', ':'))}\n\n"
    )
