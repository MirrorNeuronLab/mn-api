"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from typing import Annotated, Literal
from fastapi import APIRouter, Depends
from pydantic import Field
from mn_api.api_models import StrictModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX)


class LocalSetup(StrictModel):
    ready: bool | None = None
    mode: Literal["sample", "real", "saved"] | None = None
    revision: int = Field(default=0, ge=0)


class AssistanceEvaluation(StrictModel):
    blueprint_id: str = Field(min_length=1, max_length=128)
    job_id: str | None = Field(default=None, min_length=1, max_length=512)
    execution_id: str | None = Field(default=None, min_length=1, max_length=512)
    setup: LocalSetup = Field(default_factory=LocalSetup)
    resolved_keys: list[Annotated[str, Field(min_length=1, max_length=700)]] = Field(
        default_factory=list, max_length=200
    )
    requested_goal: Literal["schedule", "setup", "diagnose", "review", "respond", "use_own_data"] | None = None


class FailureEvidence(StrictModel):
    code: str
    summary: str


class FailedStep(StrictModel):
    label: str
    summary: str


class SchedulePresence(StrictModel):
    present: bool
    states: list[str]


class AssistanceContext(StrictModel):
    freshness: str
    run_id: str
    run_status: str
    failure: FailureEvidence
    failed_steps: list[FailedStep]
    schedule: SchedulePresence
    results_available: bool
    active_task: dict[str, str]


class AssistanceOpportunity(StrictModel):
    kind: Literal["schedule", "setup", "diagnose", "review", "respond", "use_own_data"]
    action: Literal["schedule", "setup", "diagnose", "review", "respond", "use_own_data"]
    key: str
    label: str
    reason: str
    optional: bool


class AssistanceResult(StrictModel):
    schema_name: Literal["mn.assistance.v1"] = Field(alias="schema")
    revision: str
    context: AssistanceContext
    opportunity: AssistanceOpportunity | None


@router.post(
    "/assistance/evaluations", operation_id="evaluate_assistance", tags=["assistance"], response_model=AssistanceResult
)
async def evaluate(request: AssistanceEvaluation, _principal=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("evaluate_assistance", locals())
