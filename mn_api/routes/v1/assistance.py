"""Authenticated read-only assistance; evaluation never dispatches a command."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from mn_sdk_common.assistance import evaluate_assistance
from mn_api import state
from mn_api.api_models import StrictModel
from mn_api.blueprints import find_blueprint
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth
from mn_api.job_mcp import JobContextProvider

router = APIRouter(prefix=API_PREFIX)
provider = JobContextProvider(require_response=False)


class LocalSetup(StrictModel):
    ready: bool | None = None
    mode: Literal["sample", "real", "saved"] | None = None
    revision: int = Field(default=0, ge=0)


class AssistanceEvaluation(StrictModel):
    blueprint_id: str = Field(min_length=1, max_length=128)
    job_id: str | None = Field(default=None, min_length=1, max_length=512)
    execution_id: str | None = Field(default=None, min_length=1, max_length=512)
    setup: LocalSetup = Field(default_factory=LocalSetup)
    resolved_keys: list[Annotated[str, Field(min_length=1, max_length=700)]] = Field(default_factory=list, max_length=200)
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


@router.post("/assistance/evaluations", operation_id="evaluate_assistance", tags=["assistance"], response_model=AssistanceResult)
def evaluate(request: AssistanceEvaluation, _principal=Depends(require_auth)):
    _root, blueprint = find_blueprint(state.refresh_config_from_env(), request.blueprint_id)
    context = provider.get_context(request.job_id, evidence_limit=4) if request.job_id else {"freshness": {"state": "unavailable"}}
    if request.job_id and context["identity"]["blueprint_id"] != request.blueprint_id:
        raise HTTPException(409, "The selected co-worker has changed. Refresh and try again.")
    latest = context.get("latest_run") or {}
    if request.execution_id and latest.get("run_id") != request.execution_id:
        raise HTTPException(409, "The selected run has changed. Refresh and try again.")
    metadata = blueprint.get("metadata") or {}
    worker_type = blueprint.get("type") or metadata.get("type")
    archived = (context.get("profile") or {}).get("archived") is True
    if request.job_id:
        context = {**context}
        try:
            context["pending_decisions"] = bool(provider.get_pending_human_request(request.job_id))
        except Exception:
            # An unavailable review source remains unknown, not a declaration
            # that no input is needed. Existing interaction cards remain usable.
            context["pending_decisions"] = None
    capabilities = {"setup": not archived, "diagnose": bool(request.job_id) and not archived,
                    "respond": bool(request.job_id) and not archived,
                    "review": bool(request.job_id), "schedule": worker_type == "batch" and not archived}
    return evaluate_assistance(context=context, capabilities=capabilities, setup=request.setup.model_dump(), resolved_keys=request.resolved_keys, requested_goal=request.requested_goal)
