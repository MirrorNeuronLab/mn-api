from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mn_sdk import RuntimeService, deployment_policy

from mn_api import state
from mn_api.bundles import load_uploaded_bundle
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import DeploymentActionRequest, DeploymentCreateRequest, DeploymentRollbackRequest


router = APIRouter(prefix="/api/v1")


@router.post("/deployments")
def deploy(req: DeploymentCreateRequest, _auth=Depends(require_auth)):
    try:
        manifest_json, payloads = _manifest_and_payloads(req)
        policy = deployment_policy(
            req.policy.strategy,
            req.policy.canary,
            req.policy.max_parallel,
            req.policy.auto_promote,
            req.policy.auto_revert,
        )
        return RuntimeService(state.client).deploy_job(
            manifest_json,
            payloads,
            deployment_key=req.key,
            policy=policy,
            wait=req.wait,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/deployments")
def list_deployments(_auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).list_deployments()
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/deployments/{id_or_key}")
def get_deployment(id_or_key: str, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).get_deployment(id_or_key)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/deployments/{id_or_key}/promote")
def promote_deployment(id_or_key: str, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).promote_deployment(id_or_key)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/deployments/{id_or_key}/rollback")
def rollback_deployment(id_or_key: str, req: DeploymentRollbackRequest, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).rollback_deployment(
            id_or_key,
            version=req.version or "",
            tag=req.tag,
            reason=req.reason,
        )
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/deployments/{id_or_key}/pause")
def pause_deployment(id_or_key: str, req: DeploymentActionRequest | None = None, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).pause_deployment(id_or_key, reason=(req.reason if req else ""))
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/deployments/{id_or_key}/resume")
def resume_deployment(id_or_key: str, req: DeploymentActionRequest | None = None, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).resume_deployment(id_or_key, reason=(req.reason if req else ""))
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/deployments/{id_or_key}/fail")
def fail_deployment(id_or_key: str, req: DeploymentActionRequest | None = None, _auth=Depends(require_auth)):
    try:
        return RuntimeService(state.client).fail_deployment(id_or_key, reason=(req.reason if req else ""))
    except Exception as exc:
        return handle_grpc_error(exc)


def _manifest_and_payloads(req: DeploymentCreateRequest) -> tuple[str, dict[str, bytes]]:
    if req.bundle_path:
        return load_uploaded_bundle(req.bundle_path, state.BUNDLE_UPLOAD_ROOT)
    if req.manifest_json is None:
        raise HTTPException(status_code=422, detail="manifest_json or _bundle_path is required")
    return req.manifest_json, {key: value.encode("utf-8") for key, value in (req.payloads or {}).items()}
