from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from mn_api.dependencies import require_websocket_auth
from mn_api.routes import blueprints


router = APIRouter(prefix="/api/v2")


@router.websocket("/realtime")
async def websocket_realtime(
    websocket: WebSocket,
    interval: float = Query(1.0, ge=0.25, le=30.0),
):
    await require_websocket_auth(websocket)
    await websocket.accept()
    subscriptions: dict[str, int] = {}
    heartbeat_seconds = max(float(interval), 5.0)
    last_heartbeat = time.monotonic()
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=interval)
                await handle_realtime_message(websocket, message, subscriptions)
            except asyncio.TimeoutError:
                pass

            for topic in list(subscriptions):
                sent = subscriptions[topic]
                emitted = False
                for event in realtime_events_after(topic, sent):
                    emitted = True
                    subscriptions[topic] = max(subscriptions[topic], int(event["version"]))
                    await websocket.send_json(event)
                if emitted:
                    last_heartbeat = time.monotonic()

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds:
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "serverTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "topics": sorted(subscriptions),
                    }
                )
                last_heartbeat = now
    except WebSocketDisconnect:
        pass


async def handle_realtime_message(
    websocket: WebSocket,
    message: Any,
    subscriptions: dict[str, int],
) -> None:
    if not isinstance(message, dict):
        await websocket.send_json(realtime_error("INVALID_MESSAGE", "Message must be a JSON object."))
        return

    action = str(message.get("action") or "").strip().lower()
    request_id = message.get("requestId")
    topic = str(message.get("topic") or "").strip()
    if action == "subscribe":
        if not valid_realtime_topic(topic):
            await websocket.send_json(
                realtime_error("TOPIC_NOT_FOUND", "Topic does not exist.", request_id=request_id, topic=topic)
            )
            return
        after = int_value(message.get("after"), default=0)
        subscriptions[topic] = max(after, 0)
        await websocket.send_json(
            {
                "requestId": request_id,
                "action": "subscribed",
                "topic": topic,
                "fromVersion": subscriptions[topic] + 1,
            }
        )
        return

    if action == "unsubscribe":
        subscriptions.pop(topic, None)
        await websocket.send_json({"requestId": request_id, "action": "unsubscribed", "topic": topic})
        return

    await websocket.send_json(realtime_error("INVALID_MESSAGE", "Unsupported action.", request_id=request_id, topic=topic))


def realtime_events_after(topic: str, after: int) -> list[dict[str, Any]]:
    kind, _, resource_id = topic.partition(":")
    if kind == "launch_progress" and resource_id:
        return launch_progress_events_after(resource_id, after)
    return []


def launch_progress_events_after(progress_id: str, after: int) -> list[dict[str, Any]]:
    topic = f"launch_progress:{progress_id}"
    snapshot = blueprints.launch_progress_snapshot(progress_id)
    events = snapshot.get("events") if isinstance(snapshot, dict) else []
    if not isinstance(events, list):
        return []
    payloads: list[dict[str, Any]] = []
    for sequence, event in enumerate(events, start=1):
        if sequence <= after or not isinstance(event, dict):
            continue
        status = str(event.get("status") or "")
        phase = str(event.get("phase") or "progress")
        payloads.append(
            {
                "id": f"{topic}:{sequence}",
                "topic": topic,
                "type": f"blueprint.launch_progress.{phase}.{status}".rstrip("."),
                "version": sequence,
                "occurredAt": str(event.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                "patch": {
                    "latest": event,
                    "status": snapshot.get("status"),
                    "current_phase": snapshot.get("current_phase"),
                    "completed": snapshot.get("completed"),
                    "run_id": snapshot.get("run_id"),
                    "job_id": snapshot.get("job_id"),
                    "error": snapshot.get("error"),
                },
            }
        )
    return payloads


def valid_realtime_topic(topic: str) -> bool:
    kind, _, resource_id = topic.partition(":")
    if kind == "launch_progress" and resource_id:
        return blueprints.validate_progress_id(resource_id) == resource_id
    return False


def realtime_error(
    code: str,
    message: str,
    *,
    request_id: Any = None,
    topic: str | None = None,
) -> dict[str, Any]:
    payload = {"requestId": request_id, "type": "error", "code": code, "message": message}
    if topic:
        payload["topic"] = topic
    return payload


def int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
