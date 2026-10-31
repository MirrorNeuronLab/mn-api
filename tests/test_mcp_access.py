"""External connection policy, OAuth linking, and skills over the real transport."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import AnyUrl

from mn_api.app import create_app
from mn_api.mcp_access import ConnectionStore
from tests.test_job_mcp import MCPRuntime, _configure, _mcp_request

RESOURCE = 'https://runtime.example/api/v1/jobs/job-1/mcp'


@pytest.mark.parametrize(('callback', 'target'), [
    ('https://client.example/callback', 'https://client.example:443'),
    ('http://127.0.0.1:49152/callback', 'http://127.0.0.1:49152'),
    ('http://[::1]:49152/callback', 'http://[::1]:49152'),
    ('vscode:/callback', 'vscode:'),
    ('https://user:password@client.example/callback?state=private', 'https://client.example:443'),
    ('https://client.example;script-src*/callback', 'https://client.example%3Bscript-src%2A:443'),
])
def test_consent_policy_allows_only_the_registered_callback_origin(callback, target):
    from mn_api.mcp_oauth import consent_policy
    assert consent_policy(AnyUrl(callback)) == f"default-src 'none'; form-action 'self' {target}; frame-ancestors 'none'"


def test_external_access_is_opt_in_scoped_revocable_and_preserves_internal_sessions(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime, token="owner-token")
    with TestClient(create_app(), base_url='https://runtime.example', headers={"Authorization": "Bearer owner-token"}) as client:
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
        assert client.post('/api/v1/jobs/job-1/mcp', json=request, headers={'Authorization': 'Bearer untrusted'}).status_code == 401
        internal = client.post('/api/v1/jobs/job-1/mcp-sessions').json()['access_token']
        before = client.get('/api/v1/jobs/job-1/mcp-access').json()
        assert before['enabled'] is False
        configured = client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).json()
        key = configured['connection_code']
        headers = {'Authorization': f'Bearer {key}', 'Accept': 'application/json, text/event-stream'}
        assert client.post('/api/v1/jobs/job-1/mcp', json=request, headers=headers).status_code == 200
        assert client.post('/api/v1/jobs/job-2/mcp', json=request, headers=headers).status_code == 401
        assert 'connection_code' not in client.get('/api/v1/jobs/job-1/mcp-access').text
        client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': False, 'server_url': RESOURCE})
        assert client.post('/api/v1/jobs/job-1/mcp', json=request, headers=headers).status_code == 401
        headers['Authorization'] = f'Bearer {internal}'
        assert client.post('/api/v1/jobs/job-1/mcp', json=request, headers=headers).status_code == 200
        assert key.encode() not in ConnectionStore().path.read_bytes()


def test_external_enabling_requires_owner_authentication(monkeypatch):
    _configure(monkeypatch, MCPRuntime())
    with TestClient(create_app()) as client:
        response = client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE})
        assert response.status_code == 409
        assert 'MN_API_TOKEN' in response.text
    _configure(monkeypatch, MCPRuntime(), token='owner-token')
    with TestClient(create_app()) as client:
        assert client.post('/api/v1/jobs/job-1/mcp-sessions').status_code == 401
        assert client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).status_code == 401


def test_external_clients_cannot_supply_native_launch_secrets(monkeypatch):
    _configure(monkeypatch, MCPRuntime(), token='owner-token')
    with TestClient(create_app(), headers={'Authorization': 'Bearer owner-token'}) as client:
        key = client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).json()['connection_code']
        receipt = _mcp_request(client, 'job-1', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'start_run', 'arguments': {'request_id': 'external-native'},
                       '_meta': {'mn.launch': {'secret_environment': {'TOKEN': 'private-launch-secret'}}}}}, token=key).json()
        assert receipt['result']['structuredContent']['status'] == 'failed'
        assert 'private-launch-secret' not in json.dumps(receipt)


def test_oauth_pkce_link_refresh_and_revocation(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime, token='owner-token')
    verifier = 'v' * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with TestClient(create_app(), base_url='https://runtime.example', headers={'Authorization': 'Bearer owner-token'}) as client:
        key = client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).json()['connection_code']
        discovery = client.get('/.well-known/oauth-protected-resource/api/v1/jobs/job-1/mcp').json()
        assert discovery['resource'] == RESOURCE
        auth_metadata = client.get('/.well-known/oauth-authorization-server/api/v1/mcp/oauth').json()
        assert auth_metadata['token_endpoint_auth_methods_supported'] == ['none']
        registered = client.post('/api/v1/mcp/oauth/register', json={
            'client_name': 'External AI', 'redirect_uris': ['https://client.example/callback'],
            'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'scope': 'co_worker:supervise offline_access'})
        assert registered.status_code == 201, registered.text
        client_id = registered.json()['client_id']
        authorized = client.get('/api/v1/mcp/oauth/authorize', params={
            'client_id': client_id, 'redirect_uri': 'https://client.example/callback', 'response_type': 'code',
            'scope': 'co_worker:supervise offline_access', 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'state': 'client-state', 'resource': RESOURCE}, follow_redirects=False)
        assert authorized.status_code in {302, 303, 307}, authorized.text
        location = authorized.headers['location']
        page = client.get(location)
        assert 'Allow connection' in page.text and key not in page.text
        assert page.headers['Content-Security-Policy'] == "default-src 'none'; form-action 'self' https://client.example:443; frame-ancestors 'none'"
        ticket = parse_qs(urlsplit(location).query)['ticket'][0]
        assert client.post('/api/v1/mcp/oauth/approve', data={'ticket': ticket, 'connection_code': 'wrong'}).status_code == 403
        approved = client.post('/api/v1/mcp/oauth/approve', data={'ticket': ticket, 'connection_code': key}, follow_redirects=False)
        assert approved.status_code == 303, approved.text
        query = parse_qs(urlsplit(approved.headers['location']).query)
        assert query['state'] == ['client-state']
        code = query['code'][0]
        token_body = {'grant_type': 'authorization_code', 'client_id': client_id, 'code': code,
                      'redirect_uri': 'https://client.example/callback', 'code_verifier': verifier, 'resource': RESOURCE}
        bad = client.post('/api/v1/mcp/oauth/token', data={**token_body, 'code_verifier': 'bad' * 20})
        assert bad.status_code == 400
        tokens = client.post('/api/v1/mcp/oauth/token', data=token_body)
        assert tokens.status_code == 200, tokens.text
        assert client.post('/api/v1/mcp/oauth/token', data=token_body).status_code == 400
        access = tokens.json()['access_token']
        request = {'jsonrpc': '2.0', 'id': 20, 'method': 'tools/list', 'params': {}}
        headers = {'Authorization': f'Bearer {access}', 'Accept': 'application/json, text/event-stream'}
        assert client.post('/api/v1/jobs/job-1/mcp', headers=headers, json=request).status_code == 200
        assert client.post('/api/v1/jobs/job-2/mcp', headers=headers, json=request).status_code == 401
        refresh_body = {'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': tokens.json()['refresh_token'], 'resource': RESOURCE}
        refreshed = client.post('/api/v1/mcp/oauth/token', data=refresh_body)
        assert refreshed.status_code == 200, refreshed.text
        assert client.post('/api/v1/mcp/oauth/token', data=refresh_body).status_code == 400
        client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': False, 'server_url': RESOURCE})
        assert client.post('/api/v1/jobs/job-1/mcp', headers=headers, json=request).status_code == 401


def test_skills_discovery_get_and_resource_integrity(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime)
    with TestClient(create_app()) as client:
        def call(method, **params):
            return _mcp_request(client, 'job-1', {'jsonrpc': '2.0', 'id': 12, 'method': method,
                'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                    'io.modelcontextprotocol/clientInfo': {'name': 'skills-test', 'version': '1'},
                    'io.modelcontextprotocol/clientCapabilities': {}}, **params}}).json()
        discovered = call('server/discover')
        assert 'result' in discovered, discovered
        assert 'io.modelcontextprotocol/skills' in discovered['result']['capabilities']['extensions']
        entry = call('skills/list')['result']['skills'][0]
        assert call('skills/get', uri=entry['uri'])['result']['skill'] == entry
        for resource in entry['resources']:
            result = call('resources/read', uri=resource['uri'])['result']
            raw = result['contents'][0]['text'].encode()
            assert len(raw) == resource['size']
            assert 'sha256:' + hashlib.sha256(raw).hexdigest() == resource['digest']
        skill = call('resources/read', uri=entry['uri'])['result']['contents'][0]['text']
        assert 'name: ' + entry['frontmatter']['name'] in skill
        assert 'launch credentials' in skill
        assert call('skills/get', uri='skill://other/SKILL.md')['error']['code'] == -32602
        assert call('resources/read', uri=entry['uri'] + '/../private')['error']['code'] == -32602


def test_connection_validation_rotation_and_archived_policy(monkeypatch):
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime, token='owner-token')
    with TestClient(create_app(), headers={'Authorization': 'Bearer owner-token'}) as client:
        for url in ['http://runtime.example/api/v1/jobs/job-1/mcp', RESOURCE + '?token=secret', RESOURCE.replace('job-1', 'job-2')]:
            assert client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': url}).status_code == 422
        assert client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': 'true', 'server_url': RESOURCE}).status_code == 422
        first = client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).json()['connection_code']
        second = client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).json()['connection_code']
        assert first != second
        assert not ConnectionStore().authorize('job-1', first)
        assert ConnectionStore().authorize('job-1', second)
        runtime.jobs['job-1']['status'] = 'archived'
        assert client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE}).status_code == 409
        assert client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': False, 'server_url': RESOURCE}).status_code == 200


def test_oauth_requires_pkce_valid_resource_and_supervision_scope(monkeypatch):
    _configure(monkeypatch, MCPRuntime(), token='owner-token')
    with TestClient(create_app(), base_url='https://runtime.example', headers={'Authorization': 'Bearer owner-token'}) as client:
        client.put('/api/v1/jobs/job-1/mcp-access', json={'enabled': True, 'server_url': RESOURCE})
        info = {'client_name': 'AI client', 'redirect_uris': ['https://client.example/callback'], 'token_endpoint_auth_method': 'none', 'scope': 'co_worker:supervise offline_access'}
        assert client.post('/api/v1/mcp/oauth/register', json={**info, 'token_endpoint_auth_method': 'client_secret_basic'}).status_code == 400
        client_id = client.post('/api/v1/mcp/oauth/register', json=info).json()['client_id']
        params = {'client_id': client_id, 'redirect_uri': 'https://client.example/callback', 'response_type': 'code',
                  'scope': 'co_worker:supervise', 'code_challenge': 'c'*43, 'code_challenge_method': 'S256', 'resource': RESOURCE}
        for changes in [{'resource': RESOURCE.replace('job-1', 'job-2')}, {'scope': 'offline_access'}, {'code_challenge_method': 'plain'}, {'code_challenge': None}]:
            response = client.get('/api/v1/mcp/oauth/authorize', params={key: value for key, value in {**params, **changes}.items() if value is not None}, follow_redirects=False)
            assert 'error=' in response.headers.get('location', '') or response.status_code == 400
            assert '/approve?' not in response.headers.get('location', '')


def test_native_secret_launch_is_ephemeral_idempotent_and_observed(monkeypatch, tmp_path):
    from starlette.responses import JSONResponse
    from mn_api import operations
    from mn_api.routes.v1 import blueprints
    runtime = MCPRuntime()
    _configure(monkeypatch, runtime, token='owner-token')
    monkeypatch.setenv('MN_HOME', str(tmp_path))
    # Native launch must see the same model contract on the owner and Core.
    from mn_api import blueprints as catalog
    from mn_api.routes import blueprints as legacy
    runtime.jobs['job-1']['workflow_definition'] = {'runtime': {'bindings': {}}}
    get_job = runtime.get_job
    monkeypatch.setattr(runtime, 'get_job', lambda job_id, **_: get_job(job_id))
    monkeypatch.setattr(catalog, 'find_blueprint', lambda *_: (tmp_path, {'id': 'chat-blueprint'}))
    monkeypatch.setattr(catalog, 'validate_blueprint_bundle', lambda *_: tmp_path)
    monkeypatch.setattr(legacy, 'read_manifest_for_launch', lambda *_: {'runtime': {'bindings': {}}})
    callbacks, snapshots, launches = {}, {}, []
    def start(kind, attributes, callback, *, operation_id, durable):
        assert attributes == {'job_id': 'job-1'} and durable
        callbacks[operation_id] = callback
        snapshots[operation_id] = {'operation_id': operation_id, 'status': 'running', 'progress': {'stage': 'submit'}}
        return snapshots[operation_id]
    def launch(blueprint_id, request, request_id, principal, response):
        launches.append((request.secret_environment, request.config_overrides))
        return JSONResponse({'job_id': 'job-1', 'run_id': 'execution-new', 'runtime_run_id': 'output-new', 'status': 'running'})
    monkeypatch.setattr(operations, 'start_local_operation', start)
    monkeypatch.setattr(operations, 'get_operation', lambda operation_id: snapshots[operation_id])
    monkeypatch.setattr(blueprints, 'create_blueprint_run', launch)
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'start_run', 'arguments': {'request_id': 'native-secret'},
        '_meta': {'mn.launch': {'secret_environment': {'TOKEN': 'ephemeral-launch-value'}, 'config_overrides': {'query': 'review'}}}}}
    with TestClient(create_app(), headers={'Authorization': 'Bearer owner-token'}) as client:
        def invoke(value):
            return _mcp_request(client, 'job-1', value).json()['result']['structuredContent']
        receipt = invoke(body)
        assert receipt['status'] == 'accepted'
        assert len(callbacks) == 1 and launches == []
        assert invoke(body)['operation_id'] == receipt['operation_id']
        assert len(callbacks) == 1
        backend_id = next(iter(callbacks))
        result = callbacks[backend_id](lambda **kw: None)
        snapshots[backend_id].update(status='completed', result=result)
        observed = invoke({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'get_operation_status', 'arguments': {'operation_id': receipt['operation_id']}}})
        assert observed['status'] == 'completed'
        assert observed['run_id'] == 'execution-new'
        assert observed['data']['runtime_run_id'] == 'output-new'
        assert launches == [({'TOKEN': 'ephemeral-launch-value'}, {'query': 'review'})]
        assert 'ephemeral-launch-value' not in json.dumps(observed)
        assert all(b'ephemeral-launch-value' not in path.read_bytes() for path in tmp_path.rglob('*.sqlite3'))
