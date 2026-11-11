"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX, tags=["job backups"])


class JobRestoration(BaseModel):
    job_id: str
    job_name: str | None = None
    type: str | None = None
    graph_id: str | None = None
    blueprint_id: str | None = None
    restored_from_job_id: str | None = None
    started: bool
    run_id: str | None = None
    start_error: str | None = None


@router.post("/jobs/{job_id}/backups", response_class=FileResponse, operation_id="create_job_backup")
async def create_backup(job_id: str, _auth=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_job_backup", locals())


@router.post("/job-restorations", response_model=JobRestoration, status_code=201, operation_id="restore_job_backup")
async def restore_backup(
    request: Request, start: bool = Query(False), job_id: str | None = None, _auth=Depends(require_auth)
):
    """Forward this operation to the connected runtime."""
    return await call_runtime("restore_job_backup", locals())
