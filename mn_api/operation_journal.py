"""Atomic storage of local operation receipts and their bounded SSE history."""

from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import uuid

from fastapi import HTTPException

from mn_api import state

_STATUSES = {"pending", "running", "interrupted", "completed", "failed", "cancelled", "canceled"}


def _failure(operation_id: str, category: str, status: int) -> HTTPException:
    state.logger.error("Operation receipt operation_id=%s category=%s", operation_id[:128], category)
    detail = (
        "The saved operation receipt is invalid." if status == 500 else "The operation receipt store is unavailable."
    )
    return HTTPException(status, detail)


def read(path: Path, operation_id: str) -> tuple[dict, list[dict]] | None:
    try:
        record = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _failure(operation_id, "read_unavailable", 503) from exc
    except (ValueError, UnicodeError) as exc:
        raise _failure(operation_id, "corrupt", 500) from exc

    operation = record.get("operation") if isinstance(record, dict) else None
    events = record.get("events") if isinstance(record, dict) else None
    if (
        not isinstance(operation, dict)
        or operation.get("operation_id") != operation_id
        or not isinstance(operation.get("status"), str)
        or operation["status"] not in _STATUSES
        or not isinstance(operation.get("options", {}), dict)
        or not isinstance(operation.get("progress", {}), dict)
        or not isinstance(events, list)
    ):
        raise _failure(operation_id, "corrupt", 500)
    previous = 0
    for event in events:
        if (
            not isinstance(event, dict)
            or type(event.get("sequence")) is not int
            or event["sequence"] <= previous
            or not isinstance(event.get("type"), str)
            or not isinstance(event.get("data"), dict)
        ):
            raise _failure(operation_id, "corrupt", 500)
        previous = event["sequence"]
    return operation, events[-200:]


def write(path: Path, operation: dict, events: list[dict]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8") as output:
            json.dump({"operation": operation, "events": events}, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise _failure(operation["operation_id"], "write_unavailable", 503) from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
