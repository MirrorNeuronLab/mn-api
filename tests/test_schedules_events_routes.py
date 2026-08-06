from __future__ import annotations

import json

from mn_api import state


class FakeScheduleClient:
    def __init__(self):
        self.calls = []

    def create_schedule(self, manifest_json, payloads, schedule=None, source=None):
        self.calls.append(("create_schedule", manifest_json, payloads, schedule, source))
        return json.dumps({"id": "sched-1", "schedule": schedule, "source": source})

    def list_schedules(self, kind=None, status=None):
        self.calls.append(("list_schedules", kind, status))
        return json.dumps({"data": [{"id": "sched-1", "kind": kind, "status": status}]})

    def get_schedule(self, schedule_id):
        self.calls.append(("get_schedule", schedule_id))
        return json.dumps({"id": schedule_id})

    def update_schedule(self, schedule_id, attrs, reason=""):
        self.calls.append(("update_schedule", schedule_id, attrs, reason))
        return json.dumps({"id": schedule_id, "attrs": attrs, "reason": reason})

    def pause_schedule(self, schedule_id, reason=""):
        self.calls.append(("pause_schedule", schedule_id, reason))
        return json.dumps({"id": schedule_id, "status": "paused", "reason": reason})

    def resume_schedule(self, schedule_id, reason=""):
        self.calls.append(("resume_schedule", schedule_id, reason))
        return json.dumps({"id": schedule_id, "status": "active", "reason": reason})

    def delete_schedule(self, schedule_id, reason=""):
        self.calls.append(("delete_schedule", schedule_id, reason))
        return json.dumps({"id": schedule_id, "deleted": True, "reason": reason})

    def dispatch_schedule(self, schedule_id, payload=None, reason=""):
        self.calls.append(("dispatch_schedule", schedule_id, payload, reason))
        return json.dumps({"id": schedule_id, "dispatched": True, "payload": payload, "reason": reason})

    def emit_trigger_event(self, event_type, payload=None, source=""):
        self.calls.append(("emit_trigger_event", event_type, payload, source))
        return json.dumps({"event_type": event_type, "payload": payload, "source": source})

    def list_trigger_events(self, limit=100):
        self.calls.append(("list_trigger_events", limit))
        return json.dumps({"data": [{"event_type": "ready"}], "limit": limit})


def test_schedule_shortcuts_set_expected_kind(monkeypatch, api_client):
    fake = FakeScheduleClient()
    monkeypatch.setattr(state, "client", fake)

    for path, expected_kind in (
        ("/api/v2/schedules/periodic", "periodic"),
        ("/api/v2/schedules/delayed", "delayed"),
        ("/api/v2/triggers", "event"),
    ):
        response = api_client.post(
            path,
            json={"manifest_json": '{"graph_id":"g"}', "schedule": {"name": "nightly"}, "payloads": {"a.txt": "A"}},
        )
        assert response.status_code == 200, path
        assert response.json()["schedule"]["kind"] == expected_kind
        assert fake.calls[-1][2] == {"a.txt": b"A"}


def test_schedule_mutation_routes_proxy_reasons_and_payloads(monkeypatch, api_client):
    fake = FakeScheduleClient()
    monkeypatch.setattr(state, "client", fake)

    requests = [
        ("get", "/api/v2/schedules?kind=cron&status=paused", None, ("list_schedules", "cron", "paused")),
        ("get", "/api/v2/schedules/sched-1", None, ("get_schedule", "sched-1")),
        ("patch", "/api/v2/schedules/sched-1", {"attrs": {"status": "paused"}, "reason": "manual"}, "update_schedule"),
        ("post", "/api/v2/schedules/sched-1/pause", {"reason": "manual"}, "pause_schedule"),
        ("post", "/api/v2/schedules/sched-1/resume", {"reason": "done"}, "resume_schedule"),
        ("delete", "/api/v2/schedules/sched-1?reason=retired", None, ("delete_schedule", "sched-1", "retired")),
        ("post", "/api/v2/schedules/sched-1/dispatch", {"payload": {"x": 1}, "reason": "now"}, "dispatch_schedule"),
    ]

    for method, path, body, expected in requests:
        request = getattr(api_client, method)
        response = request(path, json=body) if body is not None else request(path)
        assert response.status_code == 200, path
        if isinstance(expected, tuple):
            assert fake.calls[-1] == expected
        else:
            assert fake.calls[-1][0] == expected


def test_trigger_event_routes_proxy_runtime(monkeypatch, api_client):
    fake = FakeScheduleClient()
    monkeypatch.setattr(state, "client", fake)

    emitted = api_client.post("/api/v2/events", json={"event_type": "ready", "payload": {"ok": True}, "source": "test"})
    listed = api_client.get("/api/v2/events?limit=5")
    triggers = api_client.get("/api/v2/triggers")
    deleted = api_client.delete("/api/v2/triggers/sched-1?reason=done")

    assert emitted.status_code == 200
    assert listed.status_code == 200
    assert triggers.status_code == 200
    assert deleted.status_code == 200
    assert ("emit_trigger_event", "ready", {"ok": True}, "test") in fake.calls
    assert ("list_trigger_events", 5) in fake.calls
    assert ("list_schedules", "event", None) in fake.calls
    assert ("delete_schedule", "sched-1", "done") in fake.calls


def test_schedule_create_rejects_missing_manifest_before_sdk_call(monkeypatch, api_client):
    fake = FakeScheduleClient()
    monkeypatch.setattr(state, "client", fake)

    response = api_client.post("/api/v2/schedules", json={"schedule": {"kind": "cron"}})

    assert response.status_code == 422
    assert fake.calls == []
