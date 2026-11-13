"""REST adaptation over the SDK native runtime client; no preparation work."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import queue

import anyio
from fastapi import Request
from starlette.datastructures import UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, SecretStr

from mn_sdk.runtime_connection import connect, remove_node
from mn_sdk.runtime_control import RuntimeControlClient, decode_reply
from mn_sdk.runtime_contract.endpoints import OPERATIONS, rpc_method, rpc_timeout

STREAM_ACTIONS = {
    "stream_interactions",
    "stream_run_events",
    "stream_operation_events",
    "stream_signals",
    "download_run_artifact",
    "download_run_output",
    "create_job_backup",
    "export_run",
}


def wire_value(value):
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return wire_value(value.model_dump())
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire_value(item) for item in value]
    return value


def context_options(request):
    if request is None:
        return {}
    # Surface credentials terminate here. A separate installed credential is
    # supplied by RuntimeControlClient on the native connection.
    allowed = {"range", "if-range", "if-none-match", "accept", "content-type", "last-event-id", "x-request-id"}
    return {
        "headers": {key: value for key, value in request.headers.items() if key.lower() in allowed},
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
    }


async def call_runtime(action, values):
    if action == "get_health":
        from .config import auth_enabled
        from . import state

        return {
            "status": "ok",
            "api_contract": "mirrorneuron.rest.v1",
            "auth": "enabled" if auth_enabled(state.config) else "disabled",
        }
    if action == "create_node":
        body = values["request"]
        result = await anyio.to_thread.run_sync(
            lambda: connect(body.host, body.token, grpc_port=body.grpc_port, runtime_home=body.runtime_home)
        )
        return JSONResponse(
            result,
            status_code=201,
            headers={
                "Location": "/api/v1/nodes/"
                + str(result.get("node_name") or result.get("runtime", {}).get("node_name") or "")
            },
        )
    if action == "delete_node":
        return await anyio.to_thread.run_sync(remove_node, values["node_id"])
    item = OPERATIONS[action]
    arguments = {
        key: wire_value(value)
        for key, value in values.items()
        if key in item["parameters"] and item["parameters"][key].get("source") not in {"context", "response", "upload"}
    }
    request = next((value for value in values.values() if isinstance(value, Request)), None)
    if action in {"create_bundle", "restore_job_backup"}:
        return await upload(action, arguments, values, request)
    if action in STREAM_ACTIONS:
        return await stream(action, arguments, request)

    def invoke():
        with RuntimeControlClient(timeout=rpc_timeout(item, arguments)) as client:
            arguments["_context"] = context_options(request)
            return client.call(rpc_method(item), action, arguments, request_id=arguments.get("idempotency_key") or "")

    result = await anyio.to_thread.run_sync(invoke)
    value = decode_reply(result)
    if action == "get_job_ui" and request is not None:
        transport = (value.get("web_ui") or {}).get("transport")
        if transport:
            transport["owner_api_url"] = str(request.base_url).rstrip("/") + "/api/v1"
    if not result.value_json:
        return Response(status_code=result.status, headers=dict(result.metadata))
    return JSONResponse(value, status_code=result.status, headers=dict(result.metadata))


async def stream(action, arguments, request):
    client = RuntimeControlClient()
    arguments["_stream"] = context_options(request)
    manager = client.stream(rpc_method(OPERATIONS[action], streaming=True), action, arguments)
    try:
        frames = manager.__enter__()
        first = await anyio.to_thread.run_sync(lambda: next(frames, None))
        if first is None or first.kind != "headers" or not first.HasField("reply"):
            from mn_sdk.errors import AppError

            raise AppError("MN_RUNTIME_PROTOCOL_ERROR", "Missing runtime stream metadata.", http_status=502)
    except BaseException:
        manager.__exit__(None, None, None)
        client.close()
        raise

    async def body():
        sentinel = object()
        try:
            while True:
                frame = await anyio.to_thread.run_sync(lambda: next(frames, sentinel), abandon_on_cancel=True)
                if frame is sentinel:
                    break
                if frame.kind == "data":
                    yield frame.data
        finally:
            manager.__exit__(None, None, None)
            client.close()

    return StreamingResponse(body(), status_code=first.reply.status, headers=dict(first.reply.metadata))


async def upload(action, arguments, values, request):
    if action == "restore_job_backup" and request.headers.get("content-type", "").split(";", 1)[0] != "application/zip":
        from fastapi import HTTPException

        raise HTTPException(415, "Send the backup ZIP with Content-Type application/zip.")
    client = RuntimeControlClient(timeout=3600)
    chunks = queue.Queue(maxsize=8)
    finished = object()
    file = next((value for value in values.values() if isinstance(value, UploadFile)), None)
    arguments["_stream"] = context_options(request)
    if file:
        arguments["_filename"] = file.filename

    def source():
        while True:
            value = chunks.get()
            if value is finished:
                return
            yield value

    consumer = asyncio.create_task(asyncio.to_thread(client.upload_reply, action, arguments, source()))

    async def response():
        result = await consumer
        return JSONResponse(decode_reply(result), status_code=result.status, headers=dict(result.metadata))

    try:
        if file:
            while data := await file.read(65536):
                while chunks.full():
                    if consumer.done():
                        return await response()
                    await asyncio.sleep(0.01)
                chunks.put_nowait(data)
        else:
            async for data in request.stream():
                while chunks.full():
                    if consumer.done():
                        return await response()
                    await asyncio.sleep(0.01)
                chunks.put_nowait(data)
        while chunks.full():
            if consumer.done():
                return await response()
            await asyncio.sleep(0.01)
        chunks.put_nowait(finished)
        return await response()
    finally:
        client.close()
        # Wake a request iterator even when the upstream rejected metadata or
        # the surface client disconnected before completing its upload.
        with suppress(queue.Full):
            chunks.put_nowait(finished)
        consumer.cancel()
