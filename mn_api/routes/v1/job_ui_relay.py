"""Authenticated application adaptation for native Job UI streams."""

import asyncio
from contextlib import suppress
import hmac
import json
import queue

import anyio
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from mn_sdk.errors import AppError
from mn_sdk.job_ui_proxy import message_frames, MAX_UI_MESSAGE, close_frame, close_values, websocket_close
from mn_sdk.proto import runtime_control_pb2 as wire
from mn_sdk.runtime_control import RuntimeControlClient, request as runtime_request
from mn_api import state
from mn_api.dependencies import require_auth

router = APIRouter(prefix="/api/v1")
_END = object()


class UiConnection:
    def __init__(self, values):
        self.client = RuntimeControlClient()
        self.queue = queue.Queue(maxsize=8)
        self.values = values
        self.manager = self.client.job_ui(self.requests())
        self.frames = None
        self.closed = False

    def requests(self):
        yield wire.RuntimeFrame(version=1, kind="request", request=runtime_request("job.ui", self.values))
        while True:
            frame = self.queue.get()
            if frame is _END:
                return
            yield frame

    async def open(self):
        self.frames = self.manager.__enter__()
        try:
            return await anyio.to_thread.run_sync(next, self.frames, abandon_on_cancel=True)
        except BaseException:
            self.close()
            raise

    async def put(self, frame):
        while self.queue.full():
            if self.closed:
                return
            await asyncio.sleep(0.01)
        if not self.closed:
            self.queue.put_nowait(frame)

    async def next(self):
        return await anyio.to_thread.run_sync(lambda: next(self.frames, _END), abandon_on_cancel=True)

    def close(self):
        self.closed = True
        if self.frames is not None:
            self.manager.__exit__(None, None, None)
            self.frames = None
        self.client.close()
        with suppress(queue.Full):
            self.queue.put_nowait(_END)


def metadata(job_id, port, path, request, websocket=False):
    return {
        "job_id": job_id,
        "port": port,
        "path": path,
        "query": request.url.query,
        "websocket": websocket,
        "headers": {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in {
                "authorization",
                "proxy-authorization",
                "host",
                "cookie",
                "sec-websocket-key",
                "sec-websocket-version",
                "sec-websocket-extensions",
                "sec-websocket-protocol",
                "connection",
                "upgrade",
            }
        },
    }


@router.get("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui")
@router.head("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui_head")
@router.post("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui_post")
@router.put("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui_put")
@router.patch("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui_patch")
@router.delete("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui_delete")
@router.options("/jobs/{job_id}/ui-proxy/{port}/{path:path}", tags=["jobs"], operation_id="relay_job_ui_options")
async def relay_http(job_id: str, port: int, path: str, request: Request, _auth=Depends(require_auth)):
    connection = UiConnection({**metadata(job_id, port, path, request), "method": request.method})

    async def produce():
        try:
            async for chunk in request.stream():
                for start in range(0, len(chunk), 65536):
                    await connection.put(wire.RuntimeFrame(version=1, kind="data", data=chunk[start : start + 65536]))
        finally:
            await connection.put(_END)

    producer = asyncio.create_task(produce())
    try:
        first = await connection.open()
    except BaseException:
        producer.cancel()
        connection.close()
        raise

    async def body():
        try:
            while True:
                frame = await connection.next()
                if frame is _END:
                    break
                if frame.kind == "data":
                    yield frame.data
        finally:
            producer.cancel()
            connection.close()

    if first.kind != "headers" or not first.HasField("reply"):
        producer.cancel()
        connection.close()
        raise AppError("MN_RUNTIME_PROTOCOL_ERROR", "Missing job UI response metadata.", http_status=502)
    headers = dict(first.reply.metadata)
    cookies = json.loads(headers.pop("x-mn-set-cookies", "[]"))
    response = StreamingResponse(body(), status_code=first.reply.status, headers=headers)
    response.raw_headers.extend((b"set-cookie", cookie.encode("latin-1")) for cookie in cookies)
    return response


@router.websocket("/jobs/{job_id}/ui-proxy/{port}/ws/{path:path}")
async def relay_websocket(websocket: WebSocket, job_id: str, port: int, path: str):
    expected = state.config.api_token
    if not expected or not hmac.compare_digest(websocket.headers.get("authorization", ""), "Bearer " + expected):
        await websocket.close(code=1008)
        return
    values = metadata(job_id, port, path, websocket, True)
    values["subprotocols"] = websocket.scope.get("subprotocols", [])
    connection = UiConnection(values)
    sender = None
    close_code, close_reason = 1000, ""
    try:
        first = await connection.open()
        if first.kind != "accepted":
            await websocket.close(code=1011)
            return
        await websocket.accept(subprotocol=first.reply.metadata.get("subprotocol") or None)

        async def produce():
            outgoing_close = close_frame()
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        outgoing_close = close_frame(message.get("code", 1000), message.get("reason", ""))
                        break
                    value = message.get("text") if message.get("text") is not None else message.get("bytes", b"")
                    for frame in message_frames(value):
                        await connection.put(frame)
            finally:
                await connection.put(outgoing_close)

        sender = asyncio.create_task(produce())
        pending, kind = bytearray(), None
        while True:
            frame = await connection.next()
            if frame is _END:
                break
            if frame.kind == "close":
                close_code, close_reason = close_values(frame)
                break
            current = frame.kind.removesuffix("-part")
            if current not in {"text", "binary"} or kind not in {None, current}:
                raise ValueError("Invalid WebSocket stream")
            pending.extend(frame.data)
            kind = current
            if len(pending) > MAX_UI_MESSAGE:
                raise ValueError("Oversized WebSocket message")
            if not frame.kind.endswith("-part"):
                if kind == "text":
                    await websocket.send_text(pending.decode())
                else:
                    await websocket.send_bytes(bytes(pending))
                pending.clear()
                kind = None
    except WebSocketDisconnect as exc:
        close_code, close_reason = websocket_close(exc.code, exc.reason)
    except (AppError, ValueError):
        close_code, close_reason = 1011, "Runtime UI connection interrupted"
    finally:
        if sender:
            sender.cancel()
        connection.close()
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=close_code, reason=close_reason)
