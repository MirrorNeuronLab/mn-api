import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from mn_api import job_mcp_routing as routing


JOB = {'job_id': 'job-1', 'owner_node': 'node@spark', 'latest_run_id': 'run-1', 'revision': 2, 'data_generation': 1}
NODE = {'name': 'node@spark', 'address': '10.0.0.2', 'self?': False,
        'hardware': {'http_api': {'schema_version': 'mn.http_api.endpoint.v1', 'enabled': True, 'scheme': 'http', 'port': 54001}}}


def request(headers=None, body=b'{}'):
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/api/v1/jobs/job-1/mcp',
                    'query_string': b'', 'server': ('localhost', 54001),
                    'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}, receive)


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    routing._sessions.clear()
    monkeypatch.setattr(routing.state, 'client', SimpleNamespace(get_job=lambda _: dict(JOB),
        get_system_summary=lambda: {'nodes': [dict(NODE)]}))
    monkeypatch.setattr(routing.state, 'refresh_config_from_env', lambda: SimpleNamespace(api_token='peer-owner-token'))


def test_membership_selects_owner_and_verifies_one_hop(monkeypatch):
    base, binding = routing._location('job-1', request())
    assert base == 'http://10.0.0.2:54001/api/v1'
    with pytest.raises(HTTPException) as error:
        routing._location('job-1', request({routing.HOP_HEADER: '1', routing.BINDING_HEADER: binding}))
    assert error.value.status_code == 409
    monkeypatch.setattr(routing.state.client, 'get_system_summary', lambda: {'nodes': [{**NODE, 'self?': True}]})
    assert routing._location('job-1', request({routing.HOP_HEADER: '1', routing.BINDING_HEADER: binding})) == (None, binding)
    assert routing._location('job-1', request()) == (None, None)
    with pytest.raises(HTTPException) as error:
        routing._location('job-1', request({routing.HOP_HEADER: '1', routing.BINDING_HEADER: 'stale'}))
    assert error.value.status_code == 409


@pytest.mark.parametrize('job,nodes,headers,status', [
    ({**JOB, 'owner_node': ''}, [], {routing.HOP_HEADER: '1'}, 409),
    (JOB, [], {}, 503),
    ({**JOB, 'job_id': 'other'}, [NODE], {}, 502),
    (JOB, [NODE], {routing.HOP_HEADER: '2'}, 400),
    (JOB, [{**NODE, 'hardware': {}}], {}, 503),
])
def test_invalid_placement_fails_without_peer_request(monkeypatch, job, nodes, headers, status):
    monkeypatch.setattr(routing.state.client, 'get_job', lambda _: job)
    monkeypatch.setattr(routing.state.client, 'get_system_summary', lambda: {'nodes': nodes})
    with pytest.raises(HTTPException) as error:
        routing._location('job-1', request(headers))
    assert error.value.status_code == status


class Reply(io.BytesIO):
    status = 200
    def __init__(self, body=b'{}', headers=None):
        super().__init__(body)
        self.headers = headers or {}


@pytest.mark.parametrize('authority', ['internal', 'external'])
def test_relay_uses_peer_auth_preserves_body_and_never_upgrades_external(monkeypatch, authority):
    base, binding = routing._location('job-1', request())
    reply = Reply(b'first\nsecond', {routing.BINDING_HEADER: binding, 'Content-Type': 'text/event-stream'})
    calls = []
    monkeypatch.setattr(routing, '_open', lambda req, **kw: calls.append(req) or reply)
    req = request({'Authorization': 'Bearer client-secret', routing.EXTERNAL_HEADER: 'spoof',
                   'MCP-Protocol-Version': '2026-07-28', 'Content-Type': 'application/json'})
    response = routing._relay(base, binding, 'job-1', req, b'{"id":2}', authority)
    sent = calls[0]
    assert sent.full_url == base + '/jobs/job-1/mcp'
    assert sent.data == b'{"id":2}'
    assert sent.get_header('Authorization') == 'Bearer peer-owner-token'
    assert 'client-secret' not in str(sent.headers)
    assert sent.get_header(routing.EXTERNAL_HEADER.capitalize()) == ('1' if authority == 'external' else None)
    async def consume():
        return b''.join([chunk async for chunk in response.body_iterator])
    assert asyncio.run(consume()) == b'first\nsecond'
    assert reply.closed


def test_unconfirmed_owner_response_closes_and_fails(monkeypatch):
    reply = Reply()
    monkeypatch.setattr(routing, '_open', lambda *a, **kw: reply)
    with pytest.raises(HTTPException) as error:
        routing._relay('http://owner/api/v1', 'binding', 'job-1', request(), b'{}', 'internal')
    assert error.value.status_code == 503
    assert reply.closed


def test_no_api_key_uses_cached_existing_job_session(monkeypatch):
    monkeypatch.setattr(routing.state, 'refresh_config_from_env', lambda: SimpleNamespace(api_token=''))
    calls = []
    def open_session(req, **kw):
        calls.append(req)
        return Reply(b'{"access_token":"existing-session-token","expires_in":3600}')
    monkeypatch.setattr(routing, '_open', open_session)
    assert routing._owner_token('http://owner/api/v1', 'job-1') == 'existing-session-token'
    assert routing._owner_token('http://owner/api/v1', 'job-1') == 'existing-session-token'
    assert len(calls) == 1
    assert calls[0].full_url.endswith('/jobs/job-1/mcp-sessions')
    assert calls[0].get_header('Authorization') is None


def test_request_bound_and_network_failure_are_sanitized(monkeypatch):
    with pytest.raises(HTTPException) as error:
        asyncio.run(routing.owner_mcp_response('job-1', request(body=b'x' * (routing.MAX_BODY_BYTES + 1)), 'internal'))
    assert error.value.status_code == 413
    def fail(*args):
        raise OSError('private-address-and-token')
    monkeypatch.setattr(routing, '_relay', fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(routing.owner_mcp_response('job-1', request(), 'internal'))
    assert error.value.status_code == 503
    assert 'private-address-and-token' not in error.value.detail


def test_redirects_are_not_followed():
    assert routing._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://untrusted') is None


@pytest.mark.parametrize('external,allowed', [(False, True), (True, False)])
def test_owner_guard_confirms_binding_and_preserves_native_authority(monkeypatch, external, allowed):
    from mn_api.job_mcp import JobMCPGuard
    from mn_api.job_supervision import native_launch_authority
    from fastapi.responses import JSONResponse
    monkeypatch.setattr(routing.state, 'config', SimpleNamespace(api_token='owner-token'))
    _, binding = routing._location('job-1', request())
    monkeypatch.setattr(routing.state.client, 'get_system_summary', lambda: {'nodes': [{**NODE, 'self?': True}]})
    async def app(scope, receive, send):
        await JSONResponse({'native': native_launch_authority.get()})(scope, receive, send)
    headers = {'Authorization': 'Bearer owner-token', routing.HOP_HEADER: '1', routing.BINDING_HEADER: binding}
    if external:
        headers[routing.EXTERNAL_HEADER] = '1'
    req = request(headers)
    req.scope['path_params'] = {'job_id': 'job-1'}
    sent = []
    async def send(message):
        sent.append(message)
    guard = JobMCPGuard(app, app, app, SimpleNamespace(response_mode=lambda _: 'context'))
    asyncio.run(guard(req.scope, req.receive, send))
    assert (routing.BINDING_HEADER.lower().encode(), binding.encode()) in sent[0]['headers']
    assert sent[1]['body'] == (b'{"native":true}' if allowed else b'{"native":false}')
