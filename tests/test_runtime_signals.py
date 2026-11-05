import asyncio
import json
from types import SimpleNamespace

from mn_api import state
from mn_api.errors import app_error_response
from mn_api.routes.v1 import runtime_signals
from mn_sdk.errors import AppError


class Source:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.events)

    def close(self):
        self.closed = True


def test_sse_preserves_identity_cursor_recovery_and_transport_loss():
    events = [
        {
            "schema": "mn.runtime.event.v1",
            "channel": "errors",
            "sequence": i,
            "event_id": f"original-{i}",
            "type": kind,
            "payload": {"incident_id": "same"},
        }
        for i, kind in [(7, "runtime.error_observed"), (8, "runtime.error_recovered")]
    ]
    source = Source(map(json.dumps, events))

    async def collect():
        return [frame async for frame in runtime_signals.signal_events(source, "errors")]

    frames = asyncio.run(collect())
    assert "id: 7\n" in frames[0] and "original-7" in frames[0]
    assert "id: 8\n" in frames[1] and "runtime.error_recovered" in frames[1]
    assert "stream.unavailable" in frames[2] and "transport_unavailable" in frames[2]
    assert source.closed


def test_signal_disconnect_closes_upstream():
    source = Source([json.dumps({"type": "stream_heartbeat"})])

    async def disconnect():
        stream = runtime_signals.signal_events(source, "responses")
        assert await anext(stream) == ": heartbeat\n\n"
        await stream.aclose()

    asyncio.run(disconnect())
    assert source.closed


def test_replay_gap_requires_explicit_resynchronization():
    class Gap(Source):
        def __next__(self):
            class Error(Exception):
                def details(self):
                    return "signal_replay_gap"

            raise Error()

    source = Gap([])

    async def collect():
        return [event async for event in runtime_signals.signal_events(source, "errors")]

    events = asyncio.run(collect())
    assert "replay_required" in events[0] and '"resume_from": 0' in events[0]
    assert source.closed


def test_signal_routes_authentication_and_invalid_cursor(api_client, monkeypatch, state_snapshot):
    monkeypatch.setattr(state, "config", SimpleNamespace(**{**vars(state.config), "api_token": "required-token"}))
    response = api_client.get("/api/v1/runs/run/signals/stream?channel=errors")
    assert response.status_code == 401
    response = api_client.get(
        "/api/v1/runs/run/signals/stream?channel=errors",
        headers={"Authorization": "Bearer required-token", "Last-Event-ID": "-1"},
    )
    assert response.status_code == 400


def test_route_keeps_core_run_identity_and_resume_cursor(monkeypatch):
    calls = []
    monkeypatch.setattr(
        state,
        "client",
        SimpleNamespace(stream_events=lambda run_id, **options: calls.append((run_id, options)) or Source([])),
    )
    runtime_signals.stream_signals("core-run", "responses", "18", "user")
    assert calls[0][0] == "core-run"
    assert calls[0][1]["after_sequence"] == 18
    assert calls[0][1]["channel"] == "responses"


def test_http_overload_preserves_acceptance_and_retry_header():
    response = app_error_response(
        AppError(
            "MN_RESOURCE_EXHAUSTED",
            "Capacity is full.",
            http_status=429,
            details={"accepted": False, "command_id": "cmd", "scope": "commands", "retry_after_ms": 250},
        )
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.headers["content-type"] == "application/problem+json"
    body = json.loads(response.body)
    assert body["details"]["accepted"] is False
    assert body["details"]["command_id"] == "cmd"


def test_command_routes_preserve_identity_and_distinguish_acceptance_from_completion(api_client, monkeypatch):
    calls = []

    def send(run_id, input_id, payload, **kwargs):
        calls.append((run_id, input_id, payload, kwargs))
        return json.dumps({"accepted": True, "status": "accepted", "command_id": kwargs["idempotency_key"]})

    def read(run_id, command_id):
        calls.append((run_id, command_id))
        return json.dumps(
            {
                "accepted": True,
                "status": "completed",
                "command_id": command_id,
                "revision": 3,
                "result": {"applied": True},
            }
        )

    monkeypatch.setattr(state, "client", SimpleNamespace(send_run_input=send, get_run_input_status=read))
    response = api_client.post(
        "/api/v1/runs/run/inputs",
        json={"input_id": "adjust", "payload": {"value": 2}, "idempotency_key": "command/one"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert "result" not in response.json()
    response = api_client.get("/api/v1/runs/run/inputs/command%2Fone")
    assert response.status_code == 200
    assert response.json()["result"] == {"applied": True}
    assert calls[1] == ("run", "command/one")


def test_http_unconfirmed_acceptance_remains_unknown():
    response = app_error_response(
        AppError(
            "MN_RUNTIME_UNAVAILABLE",
            "Acceptance is unconfirmed.",
            http_status=503,
            details={"accepted": None, "command_id": "cmd", "scope": "commands"},
        )
    )
    assert response.status_code == 503
    assert json.loads(response.body)["details"]["accepted"] is None
