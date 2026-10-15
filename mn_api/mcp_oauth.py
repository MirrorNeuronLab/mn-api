"""Job-scoped OAuth linking using the MCP library's PKCE and DCR handlers."""
from __future__ import annotations

import html
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError, TokenError
from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse

from mn_api.mcp_access import ConnectionStore, digest

SCOPES = ['co_worker:supervise', 'offline_access']
OPTIONS = ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=['co_worker:supervise'])
router = APIRouter()


def issuer(resource):
    parsed = urlsplit(resource)
    return f'{parsed.scheme}://{parsed.netloc}/api/v1/mcp/oauth'


def metadata(issuer_url):
    value = build_metadata(AnyHttpUrl(issuer_url), None, OPTIONS, RevocationOptions(enabled=True))
    value.token_endpoint_auth_methods_supported = ['none']
    return value


class CoWorkerOAuth:
    @property
    def store(self):
        return ConnectionStore()

    async def get_client(self, client_id):
        record = self.store.grant(client_id, 'client')
        return OAuthClientInformationFull.model_validate(record['data']) if record else None

    async def register_client(self, client_info):
        if client_info.token_endpoint_auth_method != 'none':
            raise RegistrationError('invalid_client_metadata', 'Use a public OAuth client with PKCE and token_endpoint_auth_method=none.')
        self.store.put(client_info.client_id, 'client', lifetime=10*365*86400, data=client_info.model_dump(mode='json'))

    async def authorize(self, client, params):
        policy = self.store.policy_for_resource(params.resource)
        if not policy:
            raise AuthorizeError('invalid_target', 'This co-worker connection is disabled or the resource does not match.')
        if 'co_worker:supervise' not in (params.scopes or []) or not set(params.scopes or []).issubset(SCOPES):
            raise AuthorizeError('invalid_scope', 'Unsupported co-worker scope.')
        ticket = self.store.issue('consent', job_id=policy['job_id'], generation=policy['generation'], lifetime=600,
                                  data={'client_id': client.client_id, 'client_name': client.client_name or 'AI client', 'params': params.model_dump(mode='json')})
        return issuer(policy['server_url']) + '/approve?' + urlencode({'ticket': ticket})

    async def load_authorization_code(self, client, authorization_code):
        record = self.store.grant(authorization_code, 'code')
        if not record or record['data']['client_id'] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, expires_at=record['expires'], **record['data'])

    async def exchange_authorization_code(self, client, authorization_code):
        record = self.store.grant(authorization_code.code, 'code', consume=True)
        if not record or record['data']['client_id'] != client.client_id:
            raise TokenError('invalid_grant', 'Authorization code expired or already used.')
        return self.tokens(record['job_id'], record['generation'], client.client_id, authorization_code.scopes, authorization_code.resource)

    def tokens(self, job_id, generation, client_id, scopes, resource):
        data = {'client_id': client_id, 'scopes': scopes, 'resource': resource, 'subject': job_id}
        access = self.store.issue('access', job_id=job_id, generation=generation, lifetime=3600, data=data)
        refresh = self.store.issue('refresh', job_id=job_id, generation=generation, lifetime=30*86400, data=data)
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=3600, refresh_token=refresh, scope=' '.join(scopes))

    async def load_refresh_token(self, client, refresh_token):
        record = self.store.grant(refresh_token, 'refresh')
        if not record or record['data']['client_id'] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, expires_at=int(record['expires']), **record['data'])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if not set(scopes).issubset(refresh_token.scopes):
            raise TokenError('invalid_scope', 'Refresh cannot expand permissions.')
        record = self.store.grant(refresh_token.token, 'refresh', consume=True)
        if not record or record['data']['client_id'] != client.client_id:
            raise TokenError('invalid_grant', 'Refresh token expired or was revoked.')
        return self.tokens(record['job_id'], record['generation'], client.client_id, scopes, refresh_token.resource)

    async def load_access_token(self, token):
        record = self.store.grant(token, 'access')
        return AccessToken(token=token, expires_at=int(record['expires']), **record['data']) if record else None

    async def revoke_token(self, token):
        self.store.revoke(token.token)


@router.get('/.well-known/oauth-protected-resource/api/v1/jobs/{job_id}/mcp', include_in_schema=False)
def resource_metadata(job_id: str):
    policy = ConnectionStore().policy(job_id)
    if not policy['enabled']:
        raise HTTPException(404, 'External MCP is disabled.')
    return {'resource': policy['server_url'], 'authorization_servers': [issuer(policy['server_url'])],
            'scopes_supported': SCOPES, 'bearer_methods_supported': ['header']}


@router.get('/.well-known/oauth-authorization-server/api/v1/mcp/oauth', include_in_schema=False)
def authorization_metadata(request: Request):
    base = str(request.base_url).rstrip('/')
    return metadata(base + '/api/v1/mcp/oauth').model_dump(by_alias=True, exclude_none=True)


@router.api_route('/api/v1/mcp/oauth/approve', methods=['GET', 'POST'], include_in_schema=False)
async def approve(request: Request):
    form = await request.form() if request.method == 'POST' else request.query_params
    ticket = str(form.get('ticket') or '')
    store = ConnectionStore()
    grant = store.grant(ticket, 'consent')
    if not grant:
        raise HTTPException(400, 'This connection request expired or was revoked.')
    policy = store.policy(grant['job_id'])
    failed = False
    if request.method == 'POST':
        code = str(form.get('connection_code') or '')
        if not code or len(code) > 256 or not secrets.compare_digest(digest(code), policy['key_hash'] or ''):
            failed = True
        else:
            claimed = store.grant(ticket, 'consent', consume=True)
            if not claimed:
                raise HTTPException(400, 'This connection request was already used.')
            params = AuthorizationParams.model_validate(grant['data']['params'])
            authorization_code = store.issue('code', job_id=grant['job_id'], generation=grant['generation'], lifetime=120,
                data={'client_id': grant['data']['client_id'], 'scopes': params.scopes or ['co_worker:supervise'],
                      'code_challenge': params.code_challenge, 'redirect_uri': str(params.redirect_uri),
                      'redirect_uri_provided_explicitly': params.redirect_uri_provided_explicitly,
                      'resource': params.resource, 'subject': grant['job_id']})
            redirect = str(params.redirect_uri)
            query = {'code': authorization_code, **({'state': params.state} if params.state is not None else {})}
            return RedirectResponse(redirect + ('&' if urlsplit(redirect).query else '?') + urlencode(query), status_code=303)
    client_name = html.escape(grant['data']['client_name'])
    job_name = html.escape(grant['job_id'])
    content = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Connect co-worker</title>
    <h1>Connect {client_name}</h1><p>Allow this AI client to read work, use domain tools, and start, pause, resume, or stop runs for co-worker {job_name}.</p>
    <p>Copy the connection code from this co-worker’s MCP integration in OtterDesk. Disabling that integration revokes this connection.</p>
    {'<p role="alert">The connection code was not accepted. Try again.</p>' if failed else ''}
    <form method="post"><input type="hidden" name="ticket" value="{html.escape(ticket, quote=True)}"><label>Connection code <input name="connection_code" type="password" autocomplete="off" required maxlength="256"></label><button type="submit">Allow connection</button></form></html>'''
    return HTMLResponse(content, status_code=403 if failed else 200, headers={'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'", 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


class OAuthApp:
    """Adapt official OAuth handlers to the request's validated HTTPS/loopback issuer."""
    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        issuer_url = str(request.base_url).rstrip('/') + '/api/v1/mcp/oauth'
        provider = CoWorkerOAuth()
        routes = create_auth_routes(provider, AnyHttpUrl(issuer_url), client_registration_options=OPTIONS, revocation_options=RevocationOptions(enabled=True))
        # The path-aware RFC 8414 discovery route above is authoritative.
        routes = [route for route in routes if route.path != '/.well-known/oauth-authorization-server']
        app = Starlette(routes=routes)
        await app(scope, receive, send)
