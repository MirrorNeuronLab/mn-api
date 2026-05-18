from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from mn_api import state
from mn_api.config import auth_enabled
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error
from mn_api.schemas import ResourceSetRequest


router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health():
    return {"status": "ok", "auth": "enabled" if auth_enabled(state.config) else "disabled"}


@router.get("/system/summary")
def get_system_summary(_auth=Depends(require_auth)):
    try:
        summary_json = state.client.get_system_summary()
        return json.loads(summary_json)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/metrics")
def get_metrics(_auth=Depends(require_auth)):
    try:
        summary = json.loads(state.client.get_system_summary())
        if "metrics" in summary:
            return summary["metrics"]

        jobs = summary.get("jobs", [])
        return {
            "jobs": {
                "total": len(jobs),
                "by_status": counts(job.get("status", "unknown") for job in jobs),
            },
            "nodes": {"total": len(summary.get("nodes", []))},
            "source": "system_summary",
        }
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/resource")
def get_resource(_auth=Depends(require_auth)):
    try:
        return json.loads(state.client.get_resource())
    except Exception as exc:
        return handle_grpc_error(exc)


@router.post("/resource")
@router.put("/resource")
def set_resource(req: ResourceSetRequest, _auth=Depends(require_auth)):
    try:
        payload = req.dict(exclude_none=True)
        return json.loads(state.client.set_resource(payload))
    except Exception as exc:
        return handle_grpc_error(exc)


def counts(values):
    result = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result
