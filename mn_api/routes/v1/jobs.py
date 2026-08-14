from __future__ import annotations

import json
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from mn_sdk import RuntimeConfig, RuntimeService, generate_job_definition_submission_id, generate_stable_job_id
from mn_sdk.staged_artifacts import (
    ArtifactIntegrityError,
    ArtifactNotReadyError,
    StagedArtifactError,
    is_staged_artifact_ref,
    resolve_json_reference,
)

from mn_api import state
from mn_api.agent_graph import build_agent_graph
from mn_api.api_models import (
    HumanAcknowledgement,
    HumanResponse,
    JobBundleReplacement,
    JobCreate,
    JobUpdate,
    PageResponse,
    ResourceModel,
    RunCreate,
    RunUpdate,
    ScheduleCreate,
)
from mn_api.blueprints import create_blueprint_run_id, find_blueprint, load_blueprint_bundle
from mn_api.bundles import load_uploaded_bundle
from mn_api.contracts import API_PREFIX, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from mn_api.dependencies import require_auth
from mn_api.http_semantics import require_if_match
from mn_api.operations import encode_sse, sse_envelope, start_operation
from mn_api.pagination import page, page_tokens
from mn_api.public import decode, idempotent_response, public_value, records, resource_response
from mn_api.routes import jobs as runtime_job_routes
from mn_api.routes import runs as runtime_run_routes


router = APIRouter(prefix=API_PREFIX)
_TERMINAL = {"completed", "failed", "cancelled", "canceled", "deleted"}


def _service() -> RuntimeService:
    return RuntimeService(state.client)


def _sort_timestamp(resource: dict[str, Any]) -> str:
    return str(
        resource.get("created_at")
        or resource.get("submitted_at")
        or resource.get("started_at")
        or resource.get("updated_at")
        or ""
    )


def _revision(resource: dict[str, Any]) -> int:
    try:
        return int(resource.get("revision") or 0)
    except (TypeError, ValueError):
        return 0


def _upstream_page(
    *,
    load,
    collection_keys: tuple[str, ...],
    route: str,
    principal: str,
    filters: dict[str, Any],
    page_size: int,
    page_token: str | None,
    sort_key: str,
) -> dict[str, Any]:
    upstream_token = ""
    if page_token:
        cursor = page_tokens.resolve(
            page_token,
            route=route,
            principal=principal,
            filters=filters,
            sort_key=sort_key,
        )
        upstream_token = cursor.upstream_token or ""
    payload = decode(load(page_size, upstream_token))
    items = records(payload, "items", *collection_keys, "data")
    next_upstream = payload.get("next_page_token") if isinstance(payload, dict) else None
    next_page_token = None
    if next_upstream:
        next_page_token = page_tokens.issue(
            route=route,
            principal=principal,
            filters=filters,
            sort_key=sort_key,
            offset=0,
            snapshot=(),
            upstream_token=str(next_upstream),
        )
    return {"items": items, "next_page_token": next_page_token}


def _runtime_output_id(run_id: str) -> str:
    run = _service().get_run(run_id)
    for key in ("runtime_run_id", "runtime_job_id", "output_run_id"):
        value = run.get(key)
        if value:
            return str(value)
    for ref_key in ("result_ref", "workflow_state_ref"):
        reference = run.get(ref_key)
        if isinstance(reference, dict):
            value = reference.get("run_id") or reference.get("runtime_run_id")
            if value:
                return str(value)
    try:
        snapshot = runtime_job_routes._workflow_progress_snapshot_for_job(run_id)
        value = snapshot.get("run_id") if isinstance(snapshot, dict) else None
        if value:
            return str(value)
    except Exception:
        pass
    return run_id


def _resolve_run_result_reference(run_id: str) -> Any | None:
    run = _service().get_run(run_id)
    reference = run.get("result_ref")
    if not is_staged_artifact_ref(reference):
        result = run.get("result")
        reference = result.get("result_ref") if isinstance(result, dict) else None
    if not is_staged_artifact_ref(reference):
        return None
    resolution_env = dict(os.environ)
    resolution_env.setdefault(
        "MN_HOST_SHARED_STORAGE_ROOT",
        RuntimeConfig.from_env().shared_storage_root,
    )
    return resolve_json_reference(reference, env=resolution_env)


def _run_public(value: Any, *, run_id: str, runtime_run_id: str | None = None) -> Any:
    runtime_id = runtime_run_id or _runtime_output_id(run_id)

    def rewrite(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: rewrite(child) for key, child in item.items()}
        if isinstance(item, list):
            return [rewrite(child) for child in item]
        if isinstance(item, str) and item.startswith("/api/"):
            suffix = item.split("/artifacts/", 1)
            if len(suffix) == 2:
                return f"{API_PREFIX}/runs/{run_id}/artifacts/{suffix[1]}"
            suffix = item.split("/outputs/", 1)
            if len(suffix) == 2:
                return f"{API_PREFIX}/runs/{run_id}/outputs/{suffix[1]}"
            return item
        return item

    result = public_value(rewrite(value))
    if isinstance(result, dict):
        result["run_id"] = run_id
        if runtime_id != run_id:
            result.setdefault("runtime_run_id", runtime_id)
    return result


@router.post(
    "/jobs", status_code=status.HTTP_201_CREATED, operation_id="create_job", tags=["jobs"], response_model=ResourceModel
)
def create_job(
    request: JobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    sources = int(bool(request.blueprint_id)) + int(bool(request.bundle_id))
    if sources != 1:
        raise HTTPException(status_code=422, detail="Exactly one of blueprint_id or bundle_id is required.")

    def create():
        stable_job_id = request.job_id
        bundle_dir = None
        if request.blueprint_id:
            repo_root, blueprint = find_blueprint(state.refresh_config_from_env(), request.blueprint_id)
            bootstrap_run_id = create_blueprint_run_id(request.blueprint_id)
            stable_job_id = stable_job_id or generate_stable_job_id(request.blueprint_id)
            manifest_json, payloads = load_blueprint_bundle(
                repo_root,
                blueprint,
                bootstrap_run_id,
                config_overrides=request.resolved_configuration,
                stable_job_id=stable_job_id,
                submission_id=generate_job_definition_submission_id(stable_job_id),
            )
        else:
            manifest_json, payloads = load_uploaded_bundle(str(request.bundle_id), state.BUNDLE_UPLOAD_ROOT)
        return _service().create_stable_job(
            manifest_json,
            payloads,
            bundle_dir=bundle_dir,
            job_id=stable_job_id,
            resolved_configuration=request.resolved_configuration,
            storage=request.storage,
            idempotency_key=idempotency_key or "",
            prepared=True,
        )

    body = request.model_dump()
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/jobs",
        key=idempotency_key,
        body=body,
        call=create,
        status_code=status.HTTP_201_CREATED,
        location=lambda result: f"{API_PREFIX}/jobs/{result.get('job_id') or result.get('id')}",
    )


@router.get("/jobs", operation_id="list_jobs", tags=["jobs"], response_model=PageResponse)
def list_jobs(
    include_archived: bool = False,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    return _upstream_page(
        load=lambda size, token: _service().list_stable_jobs(
            include_archived=include_archived,
            page_size=size,
            page_token=token,
        ),
        collection_keys=("jobs",),
        route=f"{API_PREFIX}/jobs",
        principal=principal,
        filters={"include_archived": include_archived},
        page_size=page_size,
        page_token=page_token,
        sort_key="created_at,job_id",
    )


@router.get("/jobs/{job_id}", operation_id="get_job", tags=["jobs"], response_model=ResourceModel)
def get_job(job_id: str, _principal=Depends(require_auth)):
    return resource_response(_service().get_stable_job(job_id), etag=True)


@router.patch("/jobs/{job_id}", operation_id="update_job", tags=["jobs"], response_model=ResourceModel)
def update_job(
    job_id: str,
    request: JobUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_stable_job(job_id))
    require_if_match(if_match, current)
    if request.status == "archived":
        result = _service().archive_stable_job(job_id, expected_revision=_revision(current))
    else:
        attrs = {key: value for key, value in request.model_dump(exclude_none=True).items() if key != "status"}
        if request.status:
            attrs["status"] = request.status
        result = _service().update_stable_job(job_id, attrs, expected_revision=_revision(current))
    return resource_response(result, etag=True)


@router.put("/jobs/{job_id}/bundle", operation_id="replace_job_bundle", tags=["jobs"], response_model=ResourceModel)
def replace_job_bundle(
    job_id: str,
    request: JobBundleReplacement,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_stable_job(job_id))
    require_if_match(if_match, current)
    manifest_json, payloads = load_uploaded_bundle(request.bundle_id, state.BUNDLE_UPLOAD_ROOT)
    result = _service().update_stable_job(
        job_id,
        {},
        manifest_json=manifest_json,
        payloads=payloads,
        expected_revision=_revision(current),
    )
    return resource_response(result, etag=True)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_job", tags=["jobs"])
def delete_job(
    job_id: str,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _principal=Depends(require_auth),
):
    current = public_value(_service().get_stable_job(job_id))
    require_if_match(if_match, current)
    _service().delete_stable_job(job_id, confirmed=True, expected_revision=_revision(current))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/jobs/{job_id}/data-resets",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_job_data_reset",
    tags=["jobs"],
    response_model=ResourceModel,
)
def create_job_data_reset(
    job_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/jobs/{job_id}/data-resets",
        key=idempotency_key,
        body={"job_id": job_id},
        call=lambda: start_operation("reset_stable_job_data", {"job_id": job_id}),
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/operations/{result.get('operation_id') or result.get('id')}",
    )


@router.post(
    "/jobs/{job_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_job_run",
    tags=["runs"],
    response_model=ResourceModel,
)
def create_job_run(
    job_id: str,
    request: RunCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: str = Depends(require_auth),
):
    def start():
        run = _service().start_run(
            job_id,
            run_id=request.run_id,
            inputs=request.inputs,
            idempotency_key=idempotency_key or "",
        )
        run.setdefault("status", "pending")
        return run

    return idempotent_response(
        principal=principal,
        route=f"{API_PREFIX}/jobs/{job_id}/runs",
        key=idempotency_key,
        body=request.model_dump(),
        call=start,
        status_code=status.HTTP_202_ACCEPTED,
        location=lambda result: f"{API_PREFIX}/runs/{result.get('run_id') or result.get('id')}",
    )


def _all_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    jobs = records(_service().list_stable_jobs(include_archived=True), "items", "jobs", "data")
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if job_id:
            runs.extend(records(_service().list_runs(job_id), "items", "runs", "data"))
    return runs


def _page_runs(
    items: list[dict[str, Any]],
    *,
    route: str,
    principal: str,
    filters: dict[str, Any],
    page_size: int,
    page_token: str | None,
):
    return page(
        items,
        route=route,
        principal=principal,
        filters=filters,
        page_size=page_size,
        page_token=page_token,
        sort_key="-created_at,-run_id",
        key=lambda item: (_sort_timestamp(item), str(item.get("run_id") or "")),
        identity=lambda item: str(item.get("run_id") or ""),
        reverse=True,
    )


@router.get("/jobs/{job_id}/runs", operation_id="list_job_runs", tags=["runs"], response_model=PageResponse)
def list_job_runs(
    job_id: str,
    include_terminal: bool = True,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    return _upstream_page(
        load=lambda size, token: _service().list_runs(job_id, page_size=size, page_token=token),
        collection_keys=("runs",),
        route=f"{API_PREFIX}/jobs/{job_id}/runs",
        principal=principal,
        filters={"include_terminal": include_terminal},
        page_size=page_size,
        page_token=page_token,
        sort_key="created_at,run_id",
    )


@router.get("/runs", operation_id="list_runs", tags=["runs"], response_model=PageResponse)
def list_runs(
    include_terminal: bool = True,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    items = _all_runs()
    if not include_terminal:
        items = [item for item in items if str(item.get("status") or "").lower() not in _TERMINAL]
    return _page_runs(
        items,
        route=f"{API_PREFIX}/runs",
        principal=principal,
        filters={"include_terminal": include_terminal},
        page_size=page_size,
        page_token=page_token,
    )


@router.get("/runs/{run_id}", operation_id="get_run", tags=["runs"], response_model=ResourceModel)
def get_run(run_id: str, _principal=Depends(require_auth)):
    return public_value(_service().get_run(run_id))


@router.patch("/runs/{run_id}", operation_id="update_run", tags=["runs"], response_model=ResourceModel)
def update_run(run_id: str, request: RunUpdate, _principal=Depends(require_auth)):
    run = _service().get_run(run_id)
    current = str(run.get("status") or "").lower()
    desired = request.desired_state
    if current in _TERMINAL or (desired == "paused" and current not in {"pending", "running"}) or (
        desired == "running" and current != "paused"
    ):
        raise HTTPException(status_code=409, detail=f"A {current or 'unknown'} run cannot transition to {desired}.")
    if desired == "paused":
        result = _service().pause_run(run_id)
    elif desired == "running":
        result = _service().resume_run(run_id)
    else:
        result = _service().cancel_run(run_id)
    return public_value(result)


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_run", tags=["runs"])
def delete_run(run_id: str, _principal=Depends(require_auth)):
    _service().delete_run(run_id, confirmed=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/jobs/{job_id}/schedules",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_job_schedule",
    tags=["schedules"],
    response_model=ResourceModel,
)
def create_job_schedule(job_id: str, request: ScheduleCreate, response: Response, _principal=Depends(require_auth)):
    schedule = _service().create_job_schedule(job_id, schedule=request.schedule, source=request.source)
    schedule_id = str(schedule.get("schedule_id") or schedule.get("id") or "")
    if schedule_id:
        response.headers["Location"] = f"{API_PREFIX}/schedules/{schedule_id}"
    return public_value(schedule)


@router.get("/runs/{run_id}/monitor", operation_id="get_run_monitor", tags=["runs"], response_model=ResourceModel)
def get_run_monitor(run_id: str, _principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    detail = dict(runtime_job_routes._compact_job_detail(runtime_id))
    canonical_run = _service().get_run(run_id)
    canonical_status = str(canonical_run.get("status") or "").strip().lower()
    if canonical_status:
        detail["status"] = canonical_status
        for key in ("job", "summary"):
            projection = detail.get(key)
            if isinstance(projection, dict):
                detail[key] = {**projection, "status": canonical_status}
    return _run_public(detail, run_id=run_id, runtime_run_id=runtime_id)


@router.get(
    "/runs/{run_id}/workflow-progress",
    operation_id="get_run_workflow_progress",
    tags=["runs"],
    response_model=ResourceModel,
)
def get_run_workflow_progress(run_id: str, _principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    snapshot = runtime_job_routes._workflow_progress_snapshot_for_job(runtime_id)
    return _run_public(snapshot, run_id=run_id, runtime_run_id=runtime_id)


def _page_run_records(
    value: Any,
    *,
    keys: tuple[str, ...],
    route: str,
    principal: str,
    filters: dict[str, Any],
    page_size: int,
    page_token: str | None,
):
    items = records(value, *keys)
    return page(
        items,
        route=route,
        principal=principal,
        filters=filters,
        page_size=page_size,
        page_token=page_token,
        sort_key="timestamp,id",
        key=lambda item: (
            str(item.get("occurred_at") or item.get("timestamp") or item.get("ts") or ""),
            str(item.get("id") or item.get("request_id") or item.get("path") or item.get("index") or ""),
        ),
        identity=lambda item: (
            str(item.get("id") or item.get("request_id") or item.get("path") or item.get("index") or ""),
            str(item.get("occurred_at") or item.get("timestamp") or item.get("ts") or ""),
        ),
    )


@router.get("/runs/{run_id}/logs", operation_id="list_run_logs", tags=["runs"], response_model=PageResponse)
def list_run_logs(
    run_id: str,
    level: str | None = None,
    since: str | None = None,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    value = runtime_run_routes.get_run_logs(runtime_id, level, 5000, since, principal)
    return _page_run_records(
        value,
        keys=("items", "logs", "data"),
        route=f"{API_PREFIX}/runs/{run_id}/logs",
        principal=principal,
        filters={"level": level, "since": since},
        page_size=page_size,
        page_token=page_token,
    )


@router.get("/runs/{run_id}/events", operation_id="list_run_events", tags=["runs"], response_model=PageResponse)
def list_run_events(
    run_id: str,
    channel: str | None = None,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    value = runtime_run_routes.get_run_events(runtime_id, 5000, channel, principal)
    return _page_run_records(
        value,
        keys=("items", "events", "data"),
        route=f"{API_PREFIX}/runs/{run_id}/events",
        principal=principal,
        filters={"channel": channel},
        page_size=page_size,
        page_token=page_token,
    )


@router.get("/runs/{run_id}/events/stream", operation_id="stream_run_events", tags=["runs"])
def stream_run_events(
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    interval: float = Query(1.0, ge=0.25, le=30.0),
    principal: str = Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    try:
        resume_after = max(int(last_event_id or "0"), 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer.") from exc

    def source():
        emitted = resume_after
        seen: set[str] = set()
        yield ": heartbeat\n\n"
        while True:
            progress = runtime_job_routes._workflow_progress_snapshot_for_job(runtime_id)
            emitted += 1
            if emitted > resume_after:
                yield encode_sse(
                    sse_envelope(
                        event_id=emitted,
                        event_type="run.snapshot",
                        resource=f"{API_PREFIX}/runs/{run_id}",
                        data=_run_public(progress, run_id=run_id, runtime_run_id=runtime_id),
                    )
                )
            payload = runtime_run_routes.get_run_events(runtime_id, 5000, None, principal)
            for event in records(payload, "items", "events", "data"):
                identity = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
                if identity in seen:
                    continue
                seen.add(identity)
                emitted += 1
                if emitted <= resume_after:
                    continue
                event_type = str(event.get("type") or event.get("channel") or "run.event")
                yield encode_sse(
                    sse_envelope(
                        event_id=emitted,
                        event_type=event_type,
                        resource=f"{API_PREFIX}/runs/{run_id}",
                        data=event,
                    )
                )
            snapshot = _service().get_run(run_id)
            if str(snapshot.get("status") or "").lower() in _TERMINAL:
                emitted += 1
                yield encode_sse(
                    sse_envelope(
                        event_id=emitted,
                        event_type="run.completed",
                        resource=f"{API_PREFIX}/runs/{run_id}",
                        data=snapshot,
                    )
                )
                return
            yield ": heartbeat\n\n"
            time.sleep(interval)

    return StreamingResponse(
        source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/resources", operation_id="get_run_resources", tags=["runs"], response_model=ResourceModel)
def get_run_resources(run_id: str, window: str = "24h", bucket: str = "1h", principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    return _run_public(
        runtime_run_routes.get_run_resources(runtime_id, window, bucket, principal),
        run_id=run_id,
        runtime_run_id=runtime_id,
    )


@router.get(
    "/runs/{run_id}/human-requests",
    operation_id="list_run_human_requests",
    tags=["runs"],
    response_model=PageResponse,
)
def list_run_human_requests(
    run_id: str,
    request_status: str | None = Query(default=None, alias="status"),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    value = runtime_run_routes.get_run_human_events(runtime_id, request_status, principal)
    return _page_run_records(
        value,
        keys=("items", "requests", "data"),
        route=f"{API_PREFIX}/runs/{run_id}/human-requests",
        principal=principal,
        filters={"status": request_status},
        page_size=page_size,
        page_token=page_token,
    )


@router.post(
    "/runs/{run_id}/human-requests/{request_id}/responses",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_run_human_response",
    tags=["runs"],
    response_model=ResourceModel,
)
def create_run_human_response(run_id: str, request_id: str, request: HumanResponse, principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    return _run_public(
        runtime_run_routes.post_run_human_response(runtime_id, request_id, {"response": request.response}, principal),
        run_id=run_id,
        runtime_run_id=runtime_id,
    )


@router.post(
    "/runs/{run_id}/human-requests/{request_id}/acknowledgements",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_run_human_acknowledgement",
    tags=["runs"],
    response_model=ResourceModel,
)
def create_run_human_acknowledgement(
    run_id: str,
    request_id: str,
    request: HumanAcknowledgement,
    principal=Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    return _run_public(
        runtime_run_routes.post_run_human_ack(runtime_id, request_id, request.model_dump(exclude_none=True), principal),
        run_id=run_id,
        runtime_run_id=runtime_id,
    )


@router.get("/runs/{run_id}/ui", operation_id="get_run_ui", tags=["runs"], response_model=ResourceModel)
def get_run_ui(run_id: str, principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    return _run_public(runtime_run_routes.get_run_ui(runtime_id, 200, principal), run_id=run_id, runtime_run_id=runtime_id)


@router.get("/runs/{run_id}/ui/video", operation_id="get_run_ui_video", tags=["runs"])
def get_run_ui_video(run_id: str, principal=Depends(require_auth)):
    return runtime_run_routes.get_run_ui_video(_runtime_output_id(run_id), principal)


@router.get(
    "/runs/{run_id}/artifacts/final",
    operation_id="get_run_final_artifact",
    tags=["runs"],
    response_model=ResourceModel,
)
def get_run_final_artifact(run_id: str, principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    try:
        value = runtime_run_routes.get_run_final_artifact(runtime_id, principal)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        try:
            value = _resolve_run_result_reference(run_id)
        except ArtifactNotReadyError as resolve_exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "artifact_not_ready", "retryable": True, "message": str(resolve_exc)},
                headers={"Retry-After": "1"},
            ) from resolve_exc
        except ArtifactIntegrityError as resolve_exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "artifact_integrity_error", "message": str(resolve_exc)},
            ) from resolve_exc
        except StagedArtifactError as resolve_exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "artifact_resolution_error", "message": str(resolve_exc)},
            ) from resolve_exc
        if value is None:
            raise exc
    return _run_public(value, run_id=run_id, runtime_run_id=runtime_id)


@router.get("/runs/{run_id}/artifacts", operation_id="list_run_artifacts", tags=["runs"], response_model=PageResponse)
def list_run_artifacts(
    run_id: str,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    value = _run_public(runtime_run_routes.list_run_artifacts(runtime_id, principal), run_id=run_id, runtime_run_id=runtime_id)
    return _page_run_records(
        value,
        keys=("items", "artifacts"),
        route=f"{API_PREFIX}/runs/{run_id}/artifacts",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
    )


@router.get("/runs/{run_id}/artifacts/{artifact_path:path}", operation_id="download_run_artifact", tags=["runs"])
def download_run_artifact(run_id: str, artifact_path: str, principal=Depends(require_auth)):
    return runtime_run_routes.get_run_artifact(_runtime_output_id(run_id), artifact_path, principal)


@router.get("/runs/{run_id}/outputs", operation_id="list_run_outputs", tags=["runs"], response_model=PageResponse)
def list_run_outputs(
    run_id: str,
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    page_token: str | None = None,
    principal: str = Depends(require_auth),
):
    runtime_id = _runtime_output_id(run_id)
    value = _run_public(runtime_run_routes.list_run_outputs(runtime_id, principal), run_id=run_id, runtime_run_id=runtime_id)
    return _page_run_records(
        value,
        keys=("items", "outputs"),
        route=f"{API_PREFIX}/runs/{run_id}/outputs",
        principal=principal,
        filters={},
        page_size=page_size,
        page_token=page_token,
    )


@router.get("/runs/{run_id}/outputs/{output_index}", operation_id="download_run_output", tags=["runs"])
def download_run_output(run_id: str, output_index: int, principal=Depends(require_auth)):
    return runtime_run_routes.get_run_output(_runtime_output_id(run_id), output_index, principal)


@router.get(
    "/runs/{run_id}/observability",
    operation_id="get_run_observability",
    tags=["runs"],
    response_model=ResourceModel,
)
def get_run_observability(run_id: str, principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    return _run_public(
        runtime_run_routes.get_run_observability_summary(runtime_id, principal),
        run_id=run_id,
        runtime_run_id=runtime_id,
    )


@router.get("/runs/{run_id}/snapshots", operation_id="get_run_snapshot", tags=["runs"], response_model=ResourceModel)
def get_run_snapshot(run_id: str, principal=Depends(require_auth)):
    return get_run_monitor(run_id, principal)


@router.get("/runs/{run_id}/agent-graph", operation_id="get_run_agent_graph", tags=["runs"], response_model=ResourceModel)
def get_run_agent_graph(run_id: str, principal=Depends(require_auth)):
    runtime_id = _runtime_output_id(run_id)
    detail = runtime_job_routes._compact_job_detail(runtime_id)
    event_payload = runtime_run_routes.get_run_events(runtime_id, 5000, None, principal)
    graph = build_agent_graph(run_id, detail if isinstance(detail, dict) else {}, records(event_payload, "data", "events"))
    return _run_public(graph, run_id=run_id, runtime_run_id=runtime_id)


@router.get("/runs/{run_id}/export", operation_id="export_run", tags=["runs"])
def export_run(run_id: str, format: str = "json", principal=Depends(require_auth)):
    return runtime_run_routes.export_run(_runtime_output_id(run_id), format, principal)
