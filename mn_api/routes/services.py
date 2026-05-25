from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mn_api import state
from mn_api.dependencies import require_auth
from mn_api.errors import handle_grpc_error


router = APIRouter(prefix="/api/v1")


@router.get("/services")
def list_services(
    name: str | None = Query(default=None),
    node: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = Query(default=True),
    _auth=Depends(require_auth),
):
    try:
        response = state.client.list_services(
            name=name,
            node=node,
            job_id=job_id,
            agent_id=agent_id,
            status=status,
            tags=tag or [],
            passing_only=passing_only,
        )
        return json.loads(response)
    except Exception as exc:
        return handle_grpc_error(exc)


@router.get("/services/{name}/resolve")
def resolve_service(
    name: str,
    node: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    tag: Annotated[list[str] | None, Query()] = None,
    passing_only: bool = Query(default=True),
    _auth=Depends(require_auth),
):
    try:
        response = state.client.resolve_service(
            name,
            node=node,
            job_id=job_id,
            agent_id=agent_id,
            tags=tag or [],
            passing_only=passing_only,
        )
        return json.loads(response)
    except Exception as exc:
        return handle_grpc_error(exc)
