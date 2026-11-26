"""Authenticated HTTP/SSE adaptation. No process-local decision authority."""
from __future__ import annotations

import re
import json
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from mn_api import state
from mn_api.dependencies import require_auth
from mn_sdk.interactions import InteractionClient, InteractionError, PRESETS, publish_example

router = APIRouter(prefix="/api/v1", tags=["interactions"])


def service():
    return InteractionClient(state.get_client())


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except InteractionError as error:
        code = error.code
        status = 404 if code == "not_found" else 426 if code == "upgrade_required" else 503 if code == "store_unavailable" else 409 if code in {
            "closed", "expired", "revision_conflict", "identity_conflict", "idempotency_conflict", "session_closed", "cursor_expired"
        } else 422
        raise HTTPException(status_code=status, detail={"code": code, "message": "Interaction could not be updated."}) from error


class ResponseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    answer: dict = Field(default_factory=dict)


class ExampleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str


@router.get("/interactions")
def list_interactions(_principal=Depends(require_auth)):
    return invoke(service().snapshot)


@router.get("/interactions/events/stream")
async def stream_interactions(request: Request, last_event_id: Annotated[str, Header(alias="Last-Event-ID")] = "",
                              _principal=Depends(require_auth)):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", last_event_id):
        raise HTTPException(400, "Invalid interaction cursor.")
    events = service().events(last_event_id)
    sentinel = object()

    async def source():
        try:
            yield ": heartbeat\n\n"
            while not await request.is_disconnected():
                try:
                    event = await run_in_threadpool(next, events, sentinel)
                except InteractionError as error:
                    yield "event: interaction.resync\ndata: " + json.dumps({"code": error.code}) + "\n\n"
                    return
                if event is sentinel:
                    return
                if event.get("type") == "heartbeat":
                    yield ": heartbeat\n\n"
                else:
                    yield f"id: {event['id']}\nevent: interaction.updated\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # Closing the SDK iterator cancels its gRPC call.
            events.close()

    return StreamingResponse(source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/interactions/{interaction_id}")
def get_interaction(interaction_id: str, _principal=Depends(require_auth)):
    return invoke(service().get, interaction_id)


@router.post("/interactions/{interaction_id}/responses")
def respond(interaction_id: str, body: ResponseCommand,
            idempotency_key: Annotated[str, Header(min_length=1, max_length=160)], _principal=Depends(require_auth)):
    return invoke(service().respond, interaction_id, expected_revision=body.expected_revision,
                  command_id=idempotency_key, answer=body.answer)


@router.post("/interactions/{interaction_id}/acknowledgements")
def acknowledge(interaction_id: str, body: ResponseCommand,
                idempotency_key: Annotated[str, Header(min_length=1, max_length=160)], _principal=Depends(require_auth)):
    return invoke(service().acknowledge, interaction_id, expected_revision=body.expected_revision, command_id=idempotency_key)


@router.post("/interaction-test-sessions", status_code=201)
def create_session(_principal=Depends(require_auth)):
    session_id = "test-" + uuid.uuid4().hex
    record = invoke(service().publish, interaction_id=session_id, scope={"session_id": session_id}, kind="session",
                    presentation={"widget": "context", "title": "Test conversation"}, blocking=False,
                    expires_at=int(time.time() * 1000) + 1800000)
    return {"id": record["id"], "presets": PRESETS}


@router.post("/interaction-test-sessions/{session_id}/examples")
def example(session_id: str, body: ExampleCommand, _principal=Depends(require_auth)):
    return invoke(publish_example, service(), session_id, body.preset)


@router.delete("/interaction-test-sessions/{session_id}")
def close_session(session_id: str, _principal=Depends(require_auth)):
    client = service()
    record = invoke(client.get, session_id)
    if record.get("kind") != "session":
        raise HTTPException(404, "Test session not found.")
    if record["state"] == "pending":
        invoke(client.command, "cancel", id=session_id, expected_revision=record["revision"], command_id=uuid.uuid4().hex)
    return {"closed": True}
