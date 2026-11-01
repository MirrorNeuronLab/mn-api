from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from mn_api import public
from mn_api.http_semantics import IdempotencyRegistry


def test_matching_requests_share_work_and_other_keys_keep_progressing(monkeypatch):
    monkeypatch.setattr(public, "idempotency_records", IdempotencyRegistry())
    entered, release, waiting = Event(), Event(), Event()
    calls = []

    def work():
        calls.append("executed")
        entered.set()
        assert release.wait(3)
        return {"operation_id": "op-one"}

    def request(key="same", body=None, call=work):
        return public.idempotent_response(
            principal="fixture", route="/fixture", key=key, body=body or {},
            call=call, status_code=202, location=lambda value: "/operations/" + value["operation_id"],
        )

    def duplicate():
        waiting.set()
        return request()

    with ThreadPoolExecutor(max_workers=3) as pool:
        owner = pool.submit(request)
        assert entered.wait(3)
        follower = pool.submit(duplicate)
        assert waiting.wait(3)
        try:
            other = pool.submit(request, "other", {}, lambda: {"operation_id": "op-other"})
            assert other.result(timeout=2).status_code == 202
            with pytest.raises(TimeoutError):
                follower.result(timeout=0.05)
            with pytest.raises(HTTPException) as conflict:
                request(body={"different": True})
            assert conflict.value.status_code == 409
        finally:
            release.set()
        original, replay = owner.result(timeout=3), follower.result(timeout=3)
    assert calls == ["executed"]
    assert original.body == replay.body
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert original.headers["Location"] == replay.headers["Location"] == "/operations/op-one"


def test_failed_owner_releases_claim_for_an_explicit_later_request():
    registry = IdempotencyRegistry()
    options = dict(principal="fixture", route="/fixture", key="key", fingerprint="fingerprint")

    def fail():
        raise ValueError("fixture failure")

    with pytest.raises(ValueError, match="fixture failure"):
        registry.execute(**options, call=fail)
    assert registry.execute(**options, call=lambda: "retry result") == ("retry result", False)


def test_inflight_custom_response_preserves_wire_bytes_and_duplicate_headers(monkeypatch):
    monkeypatch.setattr(public, "idempotency_records", IdempotencyRegistry())
    entered, release = Event(), Event()
    response = JSONResponse({"unicode": "value"}, status_code=202)
    response.body = b'{ "unicode" : "value" }'
    response.headers["content-length"] = str(len(response.body))
    response.set_cookie("first", "one")
    response.set_cookie("second", "two")

    def work():
        entered.set()
        assert release.wait(3)
        return response

    def request():
        return public.idempotent_response(principal="fixture", route="/custom", key="key",
                                          body={}, call=work, status_code=202, location=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(request)
        assert entered.wait(3)
        follower = pool.submit(request)
        try:
            with pytest.raises(TimeoutError):
                follower.result(timeout=0.05)
        finally:
            release.set()
        original, replay = owner.result(timeout=3), follower.result(timeout=3)
    assert original.body == replay.body
    assert replay.headers.getlist("set-cookie") == original.headers.getlist("set-cookie")
    assert replay.headers["content-length"] == str(len(replay.body))
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert "Idempotency-Replayed" not in original.headers
