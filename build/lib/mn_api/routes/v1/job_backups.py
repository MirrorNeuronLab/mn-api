"""HTTP file adaptation for SDK-owned job capsules."""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from mn_api import state
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_sdk.job_backup import BackupRestoreError, backup_job, restore_job
from mn_sdk.job_backup.archive import MAX_BYTES

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
    temporary = Path(tempfile.mkdtemp(prefix="mn-api-backup-"))
    output = temporary / "job-backup.zip"
    try:
        await run_in_threadpool(backup_job, state.client, job_id, output, air_gapped=True)
        return FileResponse(output, media_type="application/zip", filename="job-backup.zip", background=BackgroundTask(shutil.rmtree, temporary))
    except BaseException as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, BackupRestoreError):
            raise HTTPException(422, detail=str(exc)) from exc
        if isinstance(exc, grpc.RpcError):
            return handle_grpc_error(exc)
        raise


@router.post("/job-restorations", response_model=JobRestoration, status_code=201, operation_id="restore_job_backup")
async def restore_backup(request: Request, start: bool = Query(False), _auth=Depends(require_auth)):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        raise HTTPException(415, detail="Send the backup ZIP with Content-Type application/zip.")
    with tempfile.TemporaryDirectory(prefix="mn-api-restore-") as temporary:
        input_path = Path(temporary) / "backup.zip"
        total = 0
        with input_path.open("xb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413, detail="Job backup exceeds 128 GiB.")
                await run_in_threadpool(output.write, chunk)
        try:
            return await run_in_threadpool(restore_job, state.client, input_path, start=start)
        except (BackupRestoreError, zipfile.BadZipFile) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        except grpc.RpcError as exc:
            return handle_grpc_error(exc)
