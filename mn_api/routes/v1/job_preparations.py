"""Short submissions for durable Job preparation, independent of HTTP lifetime."""
from __future__ import annotations

from hashlib import sha256
import json

import grpc
from fastapi import APIRouter, Depends, Header, HTTPException
from mn_sdk import generate_stable_job_id
from mn_sdk.errors import AppError

from mn_api.api_models import JobCreate, JobUpdate, ResourceModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth
from mn_api.http_semantics import idempotency_records, strong_etag
from mn_api.launch_progress import operation_progress
from mn_api.operations import get_operation, start_local_operation
from mn_api.public import resource_response
from mn_api.routes.v1 import jobs

router = APIRouter(prefix=API_PREFIX)


def _identity(principal: str, route: str, key: str | None) -> str:
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required for asynchronous work.")
    return "op-local-" + sha256(json.dumps([principal, route, key], separators=(",", ":")).encode()).hexdigest()


def _previous(operation_id: str) -> dict:
    try:
        return get_operation(operation_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return {}


def _job_if_present(job_id: str) -> dict | None:
    try:
        return jobs._service().get_job(job_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    except grpc.RpcError as exc:
        if exc.code() != grpc.StatusCode.NOT_FOUND:
            raise
    except AppError as exc:
        if exc.http_status != 404:
            raise
    return None


def _accepted(kind, operation_id, options, fingerprint, work):
    operation = start_local_operation(kind, options, work, operation_id=operation_id,
                                      request_fingerprint=fingerprint, durable=True, retry_failed=True)
    return resource_response(operation, status_code=202, location=f"{API_PREFIX}/operations/{operation_id}")


@router.post("/job-preparations", status_code=202, operation_id="prepare_job", tags=["jobs"], response_model=ResourceModel)
def prepare_job(
    request: JobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    if int(bool(request.blueprint_id)) + int(bool(request.bundle_id)) != 1:
        raise HTTPException(status_code=422, detail="Exactly one of blueprint_id or bundle_id is required.")
    operation_id = _identity(principal, f"{API_PREFIX}/job-preparations", idempotency_key)
    previous = _previous(operation_id)
    fingerprint = idempotency_records.fingerprint(request.model_dump())
    definition_fingerprint = idempotency_records.fingerprint(request.model_dump(exclude={"resolved_configuration"}))
    corrected = False
    if previous and previous.get("request_fingerprint") != fingerprint:
        if (previous.get("status") == "failed" and
                previous.get("options", {}).get("definition_fingerprint") == definition_fingerprint):
            # A corrected request gets another operation, but retains the
            # original reserved Job identity. Failed preparation must not make
            # settings fixes impossible or leave a second Job behind.
            operation_id = _identity(principal, f"{API_PREFIX}/job-preparations/{fingerprint}", idempotency_key)
            corrected = True
        else:
            # Expose the original claim for explicit reconciliation, without
            # accepting a changed payload or allocating another Job identity.
            raise HTTPException(status_code=409, detail="The request key already identifies another preparation.",
                                headers={"Location": f"{API_PREFIX}/operations/{operation_id}"})
    # Allocate in the API and save before dispatch. Reconnection and API restart
    # reuse this exact identity even if Core committed before the reply was lost.
    job_id = (previous.get("options") or {}).get("job_id") or request.job_id or generate_stable_job_id(
        request.blueprint_id or "job"
    )
    prepared_request = request.model_copy(update={"job_id": job_id})

    def work(progress):
        progress(stage="prepare", label="Prepare co-worker", detail="Preparing the saved Job definition.")
        current = _job_if_present(job_id)
        if current is not None:
            if (current.get("job_id") != job_id or
                request.blueprint_id and current.get("blueprint_id") != request.blueprint_id):
                raise HTTPException(status_code=409, detail="The prepared Job does not match this request.")
            if current.get("resolved_configuration", {}) != request.resolved_configuration:
                if not corrected:
                    raise HTTPException(status_code=409, detail="The prepared Job configuration changed.")
                with operation_progress(progress):
                    response = jobs.update_job(job_id, JobUpdate(resolved_configuration=request.resolved_configuration),
                                               strong_etag(current), principal)
                return json.loads(response.body)
            return current
        with operation_progress(progress):
            response = jobs.create_job(prepared_request, operation_id, principal, None)
        return json.loads(response.body)

    return _accepted("prepare_job", operation_id, {"job_id": job_id, "definition_fingerprint": definition_fingerprint},
                     fingerprint, work)


@router.post("/jobs/{job_id}/preparations", status_code=202, operation_id="prepare_job_update", tags=["jobs"],
             response_model=ResourceModel)
def prepare_job_update(
    job_id: str,
    request: JobUpdate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: str = Depends(require_auth),
):
    if request.status or request.resolved_configuration is None:
        raise HTTPException(status_code=422, detail="Job preparation requires configuration, without a status change.")
    if not if_match:
        raise HTTPException(status_code=428, detail="If-Match is required.")
    operation_id = _identity(principal, f"{API_PREFIX}/jobs/{job_id}/preparations", idempotency_key)
    previous = _previous(operation_id)
    expected_etag = (previous.get("options") or {}).get("expected_etag") or if_match

    def work(progress):
        progress(stage="prepare", label="Prepare changes", detail="Preparing the saved Job configuration.")
        current = jobs._service().get_job(job_id)
        # Reconcile a committed update after an interrupted HTTP/RPC response.
        # A different current configuration still goes through the original
        # conditional write and cannot overwrite another supervisor's changes.
        uncertain = previous.get("status") == "interrupted" or (
            previous.get("status") == "failed" and previous.get("error", {}).get("retryable") is True
        )
        if (uncertain and not request.refresh_contract and
                strong_etag(current) != expected_etag and
                current.get("resolved_configuration") == request.resolved_configuration):
            return current
        with operation_progress(progress):
            response = jobs.update_job(job_id, request, expected_etag, principal)
        return json.loads(response.body)

    return _accepted("prepare_job_update", operation_id, {"job_id": job_id, "expected_etag": expected_etag},
                     idempotency_records.fingerprint(request.model_dump()), work)
