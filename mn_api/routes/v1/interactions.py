"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from typing import Annotated
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from mn_api.dependencies import require_auth

router = APIRouter(prefix="/api/v1", tags=["interactions"])


class ResponseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    answer: dict = Field(default_factory=dict)


class ExampleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: str


@router.get("/interactions")
async def list_interactions(_principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("list_interactions", locals())


@router.get("/interactions/events/stream")
async def stream_interactions(
    request: Request,
    last_event_id: Annotated[str, Header(alias="Last-Event-ID")] = "",
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("stream_interactions", locals())


@router.get("/interactions/{interaction_id}")
async def get_interaction(interaction_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("get_interaction", locals())


@router.post("/interactions/{interaction_id}/responses")
async def respond(
    interaction_id: str,
    body: ResponseCommand,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=160)],
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("respond", locals())


@router.post("/interactions/{interaction_id}/acknowledgements")
async def acknowledge(
    interaction_id: str,
    body: ResponseCommand,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=160)],
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("acknowledge", locals())


@router.post("/interaction-test-sessions", status_code=201)
async def create_session(_principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_session", locals())


@router.post("/interaction-test-sessions/{session_id}/examples")
async def example(session_id: str, body: ExampleCommand, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("example", locals())


@router.delete("/interaction-test-sessions/{session_id}")
async def close_session(session_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("close_session", locals())
