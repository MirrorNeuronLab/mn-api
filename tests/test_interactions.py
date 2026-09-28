import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mn_api.routes.v1 import interactions
from mn_sdk.interactions import InteractionError

@pytest.fixture
def client(monkeypatch):
    class Service:
        commands = []
        def snapshot(self): return {"capability": "mn.interaction.v1", "cursor": "cursor", "items": []}
        def get(self, id): return {"id": id, "state": "pending", "revision": 1}
        def respond(self, id, **kwargs):
            self.commands.append((id, kwargs))
            if kwargs["expected_revision"] != 1: raise InteractionError("revision_conflict")
            return {"id": id, "state": "responded", "effect": "not_started"}
        def events(self, cursor):
            class Events:
                def __iter__(self): return self
                def __next__(self):
                    if self.done: raise StopIteration
                    self.done = True
                    return {"id": "cursor2", "type": "interaction.updated", "data": {"id": "review-1"}}
                done = False
                def close(self): self.closed = True
            return Events()
    service = Service()
    monkeypatch.setattr(interactions, "service", lambda: service)
    app = FastAPI()
    app.include_router(interactions.router)
    app.dependency_overrides[interactions.require_auth] = lambda: "test"
    return TestClient(app), service


def test_revision_and_idempotency_are_required(client):
    api, service = client
    assert api.post("/api/v1/interactions/review-1/responses", json={"expected_revision": 1, "answer": {}}).status_code == 422
    result = api.post("/api/v1/interactions/review-1/responses", headers={"Idempotency-Key": "decision-1"}, json={"expected_revision": 1, "answer": {"option_id": "approve"}})
    assert result.status_code == 200
    assert result.json()["effect"] == "not_started"
    assert service.commands[0][1]["command_id"] == "decision-1"
    conflict = api.post("/api/v1/interactions/review-1/responses", headers={"Idempotency-Key": "decision-2"}, json={"expected_revision": 2})
    assert conflict.status_code == 409


def test_stream_has_stable_ids_and_rejects_invalid_cursor(client):
    api, _ = client
    assert api.get("/api/v1/interactions/events/stream", headers={"Last-Event-ID": "bad cursor"}).status_code == 400
    result = api.get("/api/v1/interactions/events/stream", headers={"Last-Event-ID": "cursor"})
    assert result.status_code == 200
    assert "id: cursor2\n" in result.text
    assert "event: interaction.updated\n" in result.text
    data = next(line[6:] for line in result.text.splitlines() if line.startswith("data: "))
    assert json.loads(data)["data"]["id"] == "review-1"
