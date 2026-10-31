"""Relay an authenticated Job MCP connection to its Core-selected owner."""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from mn_api import state
from mn_api.node_api import node_api_base_url

HOP_HEADER = 'X-MN-MCP-Hop'
BINDING_HEADER = 'X-MN-MCP-Binding'
EXTERNAL_HEADER = 'X-MN-MCP-External'
MAX_BODY_BYTES = 1_048_576
_REQUEST_HEADERS = {'accept', 'content-type', 'mcp-protocol-version', 'mcp-method', 'mcp-name',
                    'mcp-session-id', 'last-event-id'}
_RESPONSE_HEADERS = {'content-type', 'cache-control', 'mcp-session-id', 'mcp-protocol-version', BINDING_HEADER.lower()}
_sessions = OrderedDict()
_session_lock = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class _OwnerResponse(StreamingResponse):
    def __init__(self, upstream, content, **options):
        self.upstream = upstream
        super().__init__(content, **options)

    async def __call__(self, scope, receive, send):
        async def receive_disconnect():
            message = await receive()
            if message["type"] == "http.disconnect":
                self.upstream.close()
            return message
        try:
            await super().__call__(scope, receive_disconnect, send)
        finally:
            self.upstream.close()


def _record(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _location(job_id, request):
    job = _record(state.client.get_job(job_id))
    if not isinstance(job, dict) or job.get('job_id') != job_id:
        raise HTTPException(502, 'The runtime returned a different co-worker.')
    owner = job.get('owner_node')
    hop = request.headers.get(HOP_HEADER)
    if hop not in (None, '1'):
        raise HTTPException(400, 'Invalid co-worker routing hop.')
    if not owner:
        if hop is not None:
            raise HTTPException(409, 'The co-worker owner changed. Try again.')
        return None, None
    summary = _record(state.client.get_system_summary())
    node = next((item for item in summary.get('nodes', [])
                 if isinstance(item, dict) and item.get('name') == owner), None)
    if node is None:
        raise HTTPException(503, 'The computer hosting this co-worker is unavailable.')
    binding = hashlib.sha256(json.dumps({key: job.get(key) for key in
        ('job_id', 'owner_node', 'latest_run_id', 'data_generation', 'revision')},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    local = node.get('self?') is True or node.get('self') is True
    if hop is not None:
        if not local or request.headers.get(BINDING_HEADER) != binding:
            raise HTTPException(409, 'The co-worker owner or execution changed. Try again.')
        return None, binding
    if local:
        return None, None
    return node_api_base_url(node), binding


def _open(request, timeout=65):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        return error


def _owner_token(base, job_id):
    token = str(getattr(state.refresh_config_from_env(), 'api_token', '') or '').strip()
    if token:
        return token
    # Use the same existing internal session grant as the desktop when API-key
    # authentication is not configured. Never forward an ingress client token.
    key = (base, job_id)
    with _session_lock:
        cached = _sessions.get(key)
        if cached and cached[0] > time.monotonic():
            _sessions.move_to_end(key)
            return cached[1]
        url = f'{base}/jobs/{urllib.parse.quote(job_id, safe="")}/mcp-sessions'
        with _open(urllib.request.Request(url, data=b'', method='POST'), timeout=10) as response:
            if response.status != 200:
                raise HTTPException(503, 'Could not connect to the co-worker owner.')
            payload = response.read(4097)
            if len(payload) > 4096:
                raise HTTPException(502, 'Invalid co-worker connection receipt.')
            grant = json.loads(payload)
        token = grant.get('access_token')
        lifetime = grant.get('expires_in')
        if not isinstance(token, str) or not 16 <= len(token) <= 256 or type(lifetime) is not int or not 60 <= lifetime <= 86400:
            raise HTTPException(502, 'Invalid co-worker connection receipt.')
        _sessions[key] = (time.monotonic() + lifetime - 30, token)
        _sessions.move_to_end(key)
        while len(_sessions) > 128:
            _sessions.popitem(last=False)
        return token


def _relay(base, binding, job_id, request, body, authority):
    headers = {key: value for key, value in request.headers.items() if key.lower() in _REQUEST_HEADERS}
    headers.update({HOP_HEADER: '1', BINDING_HEADER: binding, 'Accept-Encoding': 'identity',
                    'Authorization': 'Bearer ' + _owner_token(base, job_id)})
    if authority != 'internal':
        headers[EXTERNAL_HEADER] = '1'
    url = f'{base}/jobs/{urllib.parse.quote(job_id, safe="")}/mcp'
    if request.url.query:
        url += '?' + request.url.query
    upstream = _open(urllib.request.Request(url, data=body if request.method == 'POST' else None,
                                           headers=headers, method=request.method))
    if upstream.status == 409:
        upstream.close()
        raise HTTPException(409, 'The co-worker owner or execution changed. Try again.')
    if (upstream.headers.get(BINDING_HEADER) != binding or
            upstream.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity')):
        upstream.close()
        raise HTTPException(503, 'The co-worker owner could not confirm this connection. Try again.')

    def chunks():
        try:
            while data := upstream.read1(64 * 1024):
                yield data
        finally:
            upstream.close()

    return _OwnerResponse(upstream, chunks(), status_code=upstream.status,
        headers={key: value for key, value in upstream.headers.items() if key.lower() in _RESPONSE_HEADERS})


async def owner_mcp_response(job_id, request, authority):
    """Return a peer response and binding, or continue on the verified owner."""
    try:
        base, binding = await anyio.to_thread.run_sync(lambda: _location(job_id, request))
        if base is None:
            return None, binding
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise HTTPException(413, 'The co-worker request is too large.')
        response = await anyio.to_thread.run_sync(lambda: _relay(base, binding, job_id, request, bytes(body), authority))
        return response, binding
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise HTTPException(503, 'Could not connect to the co-worker owner. Try again.') from error
