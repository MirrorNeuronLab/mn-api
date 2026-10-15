"""Administrative access settings; the public MCP remains bound to one Job."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from mn_api import state
from mn_api.dependencies import require_auth
from mn_api.config import auth_enabled
from mn_api.mcp_access import ConnectionStore, valid_server_url
from mn_sdk import RuntimeService

router = APIRouter(prefix='/api/v1', tags=['jobs'])


class McpAccessUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    server_url: str = Field(max_length=2048)


class McpAccessStatus(BaseModel):
    job_id: str
    enabled: bool
    server_url: str
    authentication: Literal['oauth2']
    skills_extension: Literal['io.modelcontextprotocol/skills']


class McpAccessReceipt(McpAccessStatus):
    connection_code: str | None


class McpSessionReceipt(BaseModel):
    access_token: str
    expires_in: int


def job(job_id, *, require_mcp=True):
    value = RuntimeService(state.client).get_job(job_id)
    if value.get('deleted') or value.get('deleted_at') or require_mcp and not value.get('mcp'):
        raise HTTPException(404, 'Co-worker MCP is unavailable.')
    return value


def status(job_id, request):
    policy = ConnectionStore().policy(job_id)
    server_url = policy['server_url'] or str(request.base_url).rstrip('/') + f'/api/v1/jobs/{job_id}/mcp'
    return {'job_id': job_id, 'enabled': bool(policy['enabled']), 'server_url': server_url,
            'authentication': 'oauth2', 'skills_extension': 'io.modelcontextprotocol/skills'}


@router.get('/jobs/{job_id}/mcp-access', operation_id='get_job_mcp_access', response_model=McpAccessStatus)
def get_access(job_id: str, request: Request, _principal=Depends(require_auth)):
    job(job_id, require_mcp=False)
    return status(job_id, request)


@router.put('/jobs/{job_id}/mcp-access', operation_id='set_job_mcp_access', response_model=McpAccessReceipt)
def set_access(job_id: str, value: McpAccessUpdate, request: Request, _principal=Depends(require_auth)):
    record = job(job_id)
    if value.enabled and not auth_enabled(state.config):
        raise HTTPException(409, 'Configure MN_API_TOKEN before enabling external MCP connections.')
    if value.enabled and record.get('status') == 'archived':
        raise HTTPException(409, 'Restore the co-worker before allowing new connections.')
    try:
        valid_server_url(value.server_url, job_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    key = ConnectionStore().configure(job_id, value.enabled, value.server_url)
    # Returned only to the authenticated owner; the desktop stores this in safeStorage.
    return {**status(job_id, request), 'connection_code': key}


@router.post('/jobs/{job_id}/mcp-sessions', operation_id='create_internal_job_mcp_session', response_model=McpSessionReceipt)
def internal_session(job_id: str, _principal=Depends(require_auth)):
    job(job_id)
    token = ConnectionStore().issue('internal', job_id=job_id, lifetime=86400)
    return {'access_token': token, 'expires_in': 86400}
