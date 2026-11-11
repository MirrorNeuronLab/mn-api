"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from typing import Any, Literal
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from mn_api.api_models import ResourceModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


class RunInputRequest(BaseModel):
    input_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]


@router.post("/runs/{run_id}/inputs", tags=["runs"], response_model=ResourceModel)
async def send_input(run_id: str, request: RunInputRequest, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("send_input", locals())


@router.get("/runs/{run_id}/inputs/{command_id:path}", tags=["runs"], response_model=ResourceModel)
async def input_status(run_id: str, command_id: str, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("input_status", locals())


@router.get("/runs/{run_id}/signals/stream", tags=["runs"])
async def stream_signals(
    run_id: str,
    channel: Literal["errors", "responses"],
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _principal=Depends(require_auth),
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("stream_signals", locals())
