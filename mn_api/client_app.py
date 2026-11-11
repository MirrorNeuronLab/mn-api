"""Authenticated REST/WebSocket relay with no local runtime lifespan."""

from __future__ import annotations

import asyncio
import hmac
import os

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from mn_sdk.client_install import disconnect, load_connection, local_api_token
from mn_sdk.errors import AppError
from mn_sdk.remote_api import connect, transport_error
from mn_api.contracts import API_CONTRACT, API_PREFIX
from mn_api.config_schema import CLIENT_API_DEFAULTS

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}


class ClientNodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    token: str
    grpc_port: int | None = None


def listener() -> tuple[str, int]:
    host = os.environ.get("MN_API_HOST") or CLIENT_API_DEFAULTS["MN_API_HOST"]
    port = int(os.environ.get("MN_API_PORT") or CLIENT_API_DEFAULTS["MN_API_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("MN_API_PORT must be between 1 and 65535")
    return host, port


def _headers(headers, *, request=False):
    excluded = HOP_HEADERS | {name.strip().lower() for name in headers.get("connection", "").split(",")}
    if request:
        excluded |= {"authorization", "x-mn-install-role"}
    return {key: value for key, value in headers.items() if key.lower() not in excluded}


def _problem(status: int, code: str, detail: str, hint: str | None = None):
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://mirrorneuron.io/problems/{code}",
            "status": status,
            "title": code.replace("_", " "),
            "code": code,
            "detail": detail,
            "instance": "",
            "request_id": "",
            **({"hint": hint} if hint else {}),
        },
    )


def create_client_app(*, transport=None, connection_transport=None, websocket_connect=None) -> FastAPI:
    # Validate at startup, before accepting unauthenticated access to credentials.
    local_api_token()
    app = FastAPI(title="MirrorNeuron client API", openapi_url=None, docs_url=None, redoc_url=None)
    origins = [s.strip() for s in os.environ.get("MN_API_CORS_ALLOW_ORIGINS", "").split(",") if s.strip()]

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return _problem(exc.http_status, exc.code, exc.user_message, exc.hint)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        # Pydantic's raw error includes the supplied connection token.
        return _problem(422, "validation_failed", "The connection request is invalid.")

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return _problem(exc.status_code, "request_failed", str(exc.detail))

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        response = None
        if request.url.path != API_PREFIX + "/health":
            try:
                if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + local_api_token()):
                    response = _problem(401, "authentication_required", "A valid local API bearer token is required.")
            except AppError as exc:
                response = _problem(exc.http_status, exc.code, exc.user_message, exc.hint)
        if response is None:
            response = await call_next(request)
        response.headers["X-API-Contract"] = API_CONTRACT
        response.headers["X-MN-Install-Role"] = "client"
        return response

    @app.get(API_PREFIX + "/health")
    def health():
        return {"status": "ok", "api_contract": API_CONTRACT, "auth": "enabled"}

    @app.post(API_PREFIX + "/nodes", status_code=201)
    async def add_node(body: ClientNodeCreate):
        if body.grpc_port is not None:
            raise HTTPException(422, "Client mode connects to an API URL; omit grpc_port.")
        result = await run_in_threadpool(connect, body.host, body.token, transport=connection_transport)
        from mn_sdk.remote_api import resource_id

        return JSONResponse(
            result, status_code=201, headers={"Location": API_PREFIX + "/nodes/" + resource_id(result["node_name"])}
        )

    @app.delete(API_PREFIX + "/nodes/{node_name}")
    async def remove_node(node_name: str):
        return await run_in_threadpool(disconnect, node_name)

    @app.api_route(API_PREFIX + "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def relay(request: Request, path: str):
        connection = load_connection()
        # Use the raw encoded resource path so opaque IDs survive forwarding.
        suffix = request.scope["raw_path"].decode("ascii").split("?", 1)[0][len(API_PREFIX) :]
        target = connection.api_url + suffix
        if request.url.query:
            target += "?" + request.url.query
        headers = _headers(request.headers, request=True)
        headers["Authorization"] = "Bearer " + connection.token
        client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, timeout=httpx.Timeout(None, connect=10, pool=10)
        )
        try:
            upstream_request = client.build_request(request.method, target, headers=headers, content=request.stream())
            upstream = await client.send(upstream_request, stream=True)
        except httpx.RequestError:
            await client.aclose()
            raise transport_error() from None
        except BaseException:
            await client.aclose()
            raise

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await client.aclose()

        response_headers = _headers(upstream.headers)
        location = response_headers.get("location", "")
        if location.startswith(connection.api_url + "/"):
            response_headers["location"] = API_PREFIX + location[len(connection.api_url) :]
        response = StreamingResponse(body(), status_code=upstream.status_code)
        # Set-Cookie and other repeatable response headers must remain separate,
        # including session cookies used by remote job UIs.
        response.raw_headers = [
            (
                name.encode("latin-1"),
                (response_headers[name] if name == "location" else value).encode("latin-1"),
            )
            for name, value in upstream.headers.multi_items()
            if name in response_headers
        ]
        return response

    @app.websocket(API_PREFIX + "/{path:path}")
    async def relay_websocket(socket: WebSocket, path: str):
        if not hmac.compare_digest(socket.headers.get("authorization", ""), "Bearer " + local_api_token()):
            await socket.close(code=1008)
            return
        tasks = []
        try:
            import websockets

            connection = load_connection()
            suffix = socket.scope["raw_path"].decode("ascii").split("?", 1)[0][len(API_PREFIX) :]
            target = connection.api_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + suffix
            if socket.url.query:
                target += "?" + socket.url.query
            headers = _headers(socket.headers, request=True)
            headers = {k: v for k, v in headers.items() if not k.lower().startswith("sec-websocket-")}
            headers["Authorization"] = "Bearer " + connection.token

            # Never forward the stored bearer token to a redirected origin.
            class DirectConnection(websockets.connect):
                def process_redirect(self, exc):
                    return exc

            connector = websocket_connect or DirectConnection
            async with connector(
                target, additional_headers=headers, subprotocols=socket.scope.get("subprotocols", []), open_timeout=10
            ) as upstream:
                await socket.accept(subprotocol=upstream.subprotocol)

                async def to_remote():
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(
                            message.get("bytes") if message.get("bytes") is not None else message["text"]
                        )

                async def to_local():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await socket.send_bytes(message)
                        else:
                            await socket.send_text(message)

                tasks = [asyncio.create_task(to_remote()), asyncio.create_task(to_local())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except Exception:
            try:
                await socket.close(code=1011)
            except RuntimeError:
                pass
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await socket.close()
                except RuntimeError:
                    pass

    # CORS must wrap authentication so browser preflights can succeed without
    # credentials. The subsequent API request still requires its bearer token.
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"], allow_credentials=True
        )
    return app
