"""Protected runtime signals; no snapshot polling or bulk event buffering."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mn_api import state
from mn_api.api_models import ResourceModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth
from mn_api.public import decode, public_value

router = APIRouter(prefix=API_PREFIX)


class RunInputRequest(BaseModel):
    input_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]


@router.post("/runs/{run_id}/inputs", tags=["runs"], response_model=ResourceModel)
def send_input(run_id: str, request: RunInputRequest, _principal=Depends(require_auth)):
    return public_value(decode(state.client.send_run_input(
        run_id, request.input_id, request.payload, idempotency_key=request.idempotency_key,
    )))


@router.get("/runs/{run_id}/inputs/{command_id:path}", tags=["runs"], response_model=ResourceModel)
def input_status(run_id: str, command_id: str, _principal=Depends(require_auth)):
    return public_value(decode(state.client.get_run_input_status(run_id, command_id)))


def _next_event(upstream):
    # StopIteration cannot be propagated through an asyncio Future.
    return next(upstream, None)


async def signal_events(upstream, channel: str):
    try:
        while True:
            raw = await asyncio.to_thread(_next_event, upstream)
            if raw is None:
                raise RuntimeError("Signal transport ended")
            event = public_value(decode(raw))
            if event.get("type") == "stream_heartbeat":
                yield ": heartbeat\n\n"
                continue
            sequence = event.get("sequence")
            if not isinstance(sequence, int) or sequence <= 0 or event.get("channel") != channel:
                raise ValueError("Invalid signal event")
            # The Core identity and durable position survive every adapter.
            yield f'id: {sequence}\nevent: runtime.signal\ndata: {json.dumps(event, separators=(",", ":"))}\n\n'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        details = getattr(exc, "details", "")
        if callable(details):
            details = details()
        gap = "signal_replay_gap" in str(details)
        notice = {"channel": channel, "code": "replay_required" if gap else "transport_unavailable",
                  "resume_from": 0 if gap else None}
        # Transport loss is never an operation failure or successful delivery.
        yield f'event: stream.unavailable\ndata: {json.dumps(notice)}\n\n'
    finally:
        upstream.close()


@router.get("/runs/{run_id}/signals/stream", tags=["runs"])
def stream_signals(
    run_id: str,
    channel: Literal["errors", "responses"],
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _principal=Depends(require_auth),
):
    try:
        cursor = int(last_event_id or "0")
        if cursor < 0:
            raise ValueError()
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID must be a nonnegative integer.") from exc
    upstream = state.client.stream_events(run_id, channel=channel, after_sequence=cursor,
                                          follow=True, timeout=None, heartbeat_interval_ms=1000)
    return StreamingResponse(signal_events(upstream, channel), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
