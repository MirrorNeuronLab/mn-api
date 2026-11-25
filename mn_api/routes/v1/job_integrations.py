"""Authenticated administration of prepared skills and per-job integrations."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Header
from mn_api.dependencies import require_auth
from mn_sdk_shared.runtime_contract.job_integrations import DesktopDelivery, SlackConnection, SkillEnablement
from mn_api.runtime_adapter import call_runtime

router = APIRouter(prefix="/api/v1/jobs/{job_id}", tags=["jobs"])


def owner(principal=Depends(require_auth)):
    if principal != "authenticated":
        raise HTTPException(401, "Configure runtime API authentication to manage job integrations.")
    return principal


@router.get("/skills", operation_id="list_job_skills", status_code=200)
async def list_job_skills(job_id: str, principal=Depends(owner)):
    return await call_runtime("list_job_skills", locals())


@router.post("/skills", operation_id="add_job_skill", status_code=202)
async def add_job_skill(
    job_id: str,
    value: dict,
    principal=Depends(owner),
    if_match: str | None = Header(None),
    idempotency_key: str | None = Header(None),
):
    return await call_runtime("add_job_skill", locals())


@router.put("/skills/{name}", operation_id="set_job_skill", status_code=200)
async def set_job_skill(job_id: str, name: str, value: SkillEnablement, principal=Depends(owner)):
    return await call_runtime("set_job_skill", locals())


@router.get("/integrations", operation_id="list_job_integrations", status_code=200)
async def list_job_integrations(job_id: str, principal=Depends(owner)):
    return await call_runtime("list_job_integrations", locals())


@router.put("/integrations/slack", operation_id="add_job_slack", status_code=200)
async def add_job_slack(job_id: str, principal=Depends(owner)):
    return await call_runtime("add_job_slack", locals())


@router.post("/integrations/slack/connect", operation_id="connect_job_slack", status_code=200)
async def connect_job_slack(
    job_id: str, value: SlackConnection, principal=Depends(owner), idempotency_key: str | None = Header(None)
):
    return await call_runtime("connect_job_slack", locals())


@router.post("/integrations/slack/{operation}", operation_id="control_job_slack", status_code=200)
async def control_job_slack(
    job_id: str, operation: Literal["pause", "disconnect", "cancel"], value: SlackConnection, principal=Depends(owner)
):
    return await call_runtime("control_job_slack", locals())


@router.post("/integrations/slack/desktop/{action}", operation_id="deliver_job_slack", status_code=200)
async def deliver_job_slack(
    job_id: str, action: Literal["receive", "reply", "alert", "sleep"], value: DesktopDelivery, principal=Depends(owner)
):
    return await call_runtime("deliver_job_slack", locals())


@router.get("/skills/enabled/files", operation_id="enabled_job_skill_files", status_code=200)
async def enabled_job_skill_files(job_id: str, principal=Depends(owner)):
    return await call_runtime("enabled_job_skill_files", locals())
