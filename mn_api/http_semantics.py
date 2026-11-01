from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from hashlib import sha256
import json
import threading
import time
from typing import Any, Callable, Mapping

from fastapi import HTTPException

from mn_api.contracts import IDEMPOTENCY_TTL_SECONDS


def strong_etag(resource: Any) -> str:
    encoded = json.dumps(resource, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f'"{sha256(encoded).hexdigest()}"'


def require_if_match(if_match: str | None, resource: Any) -> None:
    if not if_match:
        raise HTTPException(status_code=428, detail="If-Match is required for this resource.")
    if if_match.strip() != strong_etag(resource):
        raise HTTPException(status_code=412, detail="The resource changed since it was read.")


@dataclass(frozen=True)
class Replay:
    fingerprint: str
    status_code: int
    headers: Mapping[str, str]
    body: Any
    expires_at: float


class IdempotencyRegistry:
    def __init__(self, *, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS, maximum: int = 10_000):
        self._ttl_seconds = ttl_seconds
        self._maximum = maximum
        self._records: OrderedDict[tuple[str, str, str], Replay] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, str, str], tuple[str, Future]] = {}

    def execute(self, *, principal: str, route: str, key: str, fingerprint: str,
                call: Callable[[], Any]) -> tuple[Any, bool]:
        """Coalesce an in-flight request without serializing unrelated keys.

        Completed responses retain the existing replay policy. Failures are
        shared with current waiters, but never replayed as successful results.
        """
        identity = (principal, route, key)
        with self._lock:
            self._prune(time.time())
            replay = self._records.get(identity)
            active = self._inflight.get(identity)
            previous = replay.fingerprint if replay is not None else active[0] if active else None
            if previous is not None and previous != fingerprint:
                raise HTTPException(status_code=409, detail="The Idempotency-Key was already used with another request.")
            if replay is not None:
                self._records.move_to_end(identity)
                return replay, True
            owner = active is None
            future = Future() if owner else active[1]
            if owner:
                self._inflight[identity] = (fingerprint, future)
        if not owner:
            return future.result(), True
        try:
            result = call()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return result, False
        finally:
            with self._lock:
                self._inflight.pop(identity, None)

    @staticmethod
    def fingerprint(body: Any) -> str:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return sha256(encoded).hexdigest()

    def find(self, *, principal: str, route: str, key: str, fingerprint: str) -> Replay | None:
        now = time.time()
        record_key = (principal, route, key)
        with self._lock:
            self._prune(now)
            replay = self._records.get(record_key)
            if replay is not None:
                self._records.move_to_end(record_key)
        if replay is None:
            return None
        if replay.fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="The Idempotency-Key was already used with another request.")
        return replay

    def store(
        self,
        *,
        principal: str,
        route: str,
        key: str,
        fingerprint: str,
        status_code: int,
        headers: Mapping[str, str],
        body: Any,
    ) -> None:
        record_key = (principal, route, key)
        replay = Replay(
            fingerprint=fingerprint,
            status_code=status_code,
            headers=dict(headers),
            body=body,
            expires_at=time.time() + self._ttl_seconds,
        )
        with self._lock:
            self._prune(time.time())
            self._records[record_key] = replay
            while len(self._records) > self._maximum:
                self._records.popitem(last=False)

    def _prune(self, now: float) -> None:
        expired = [key for key, replay in self._records.items() if replay.expires_at <= now]
        for key in expired:
            self._records.pop(key, None)


idempotency_records = IdempotencyRegistry()

