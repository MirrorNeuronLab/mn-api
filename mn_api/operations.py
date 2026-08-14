from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import threading
from typing import Any
import uuid

from mn_api import state
from mn_api.public import decode, first_identifier, public_value


_MAX_OPERATIONS = 10_000
_operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
_lock = threading.Lock()


def register_operation(value: Any) -> dict[str, Any]:
    resource = public_value(decode(value))
    if not isinstance(resource, dict):
        resource = {"status": "accepted"}
    operation_id = first_identifier(resource, ("operation_id", "id"))
    if operation_id:
        resource.setdefault("operation_id", operation_id)
        with _lock:
            _operations[operation_id] = resource
            _operations.move_to_end(operation_id)
            while len(_operations) > _MAX_OPERATIONS:
                _operations.popitem(last=False)
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

    Blueprint catalog and installation work is owned by mn-api because it uses
    the API host's configured catalog and model registry. Core must not be
    asked to execute those filesystem-scoped operations.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return register_operation(
        {
            "operation_id": f"op-local-{uuid.uuid4().hex}",
            "kind": kind,
            "status": "completed",
            "created_at": now,
            "updated_at": now,
            "options": public_value(options or {}),
            "result": public_value(result),
        }
    )


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
        return list(_operations.values())


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
