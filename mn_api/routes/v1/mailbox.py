"""Authenticated REST access to Core's stable Job mailboxes."""

from fastapi import APIRouter, Depends, Query

from mn_sdk.runtime_contract.mailbox import Digest, Kind, MailboxMessage, MailboxPage, MailboxSend
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth
from mn_api.runtime_adapter import call_runtime

router = APIRouter(prefix=API_PREFIX, tags=["jobs"])


@router.get("/jobs/{job_id}/mailbox", operation_id="list_job_mailbox", response_model=MailboxPage)
async def list_job_mailbox(
    job_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=50),
    include_acknowledged: bool = False,
    kind: Kind | None = None,
    reply_to: Digest | None = None,
    _principal=Depends(require_auth),
):
    return await call_runtime("list_job_mailbox", locals())


@router.post("/jobs/{job_id}/mailbox", operation_id="send_job_mail", response_model=MailboxMessage)
async def send_job_mail(job_id: str, request: MailboxSend, _principal=Depends(require_auth)):
    return await call_runtime("send_job_mail", locals())


@router.put(
    "/jobs/{job_id}/mailbox/{message_id}/acknowledgement",
    operation_id="acknowledge_job_mail",
    response_model=MailboxMessage,
)
async def acknowledge_job_mail(
    job_id: str, message_id: Digest, expected_kind: Kind | None = None, _principal=Depends(require_auth)
):
    return await call_runtime("acknowledge_job_mail", locals())
