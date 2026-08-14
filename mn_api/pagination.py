from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
import secrets
import threading
import time
from typing import Any, Callable, Iterable, Mapping, TypeVar

from fastapi import HTTPException

from mn_api.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PAGE_TOKEN_TTL_SECONDS


T = TypeVar("T")


@dataclass(frozen=True)
class _Cursor:
    route: str
    principal: str
    filters_hash: str
    sort_key: str
    offset: int
    snapshot: tuple[str, ...]
    upstream_token: str | None
    expires_at: float


class PageTokenRegistry:
    """Bounded, process-local registry for opaque continuation tokens."""

    def __init__(self, *, ttl_seconds: int = PAGE_TOKEN_TTL_SECONDS, maximum: int = 10_000):
        self._ttl_seconds = ttl_seconds
        self._maximum = maximum
        self._tokens: OrderedDict[str, _Cursor] = OrderedDict()
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        route: str,
        principal: str,
        filters: Mapping[str, Any],
        sort_key: str,
        offset: int,
        snapshot: tuple[str, ...],
        upstream_token: str | None = None,
        now: float | None = None,
    ) -> str:
        current = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        cursor = _Cursor(
            route=route,
            principal=principal,
            filters_hash=_filters_hash(filters),
            sort_key=sort_key,
            offset=offset,
            snapshot=snapshot,
            upstream_token=upstream_token,
            expires_at=current + self._ttl_seconds,
        )
        with self._lock:
            self._prune(current)
            self._tokens[token] = cursor
            while len(self._tokens) > self._maximum:
                self._tokens.popitem(last=False)
        return token

    def resolve(
        self,
        token: str,
        *,
        route: str,
        principal: str,
        filters: Mapping[str, Any],
        sort_key: str,
        now: float | None = None,
    ) -> _Cursor:
        current = time.time() if now is None else now
        with self._lock:
            self._prune(current)
            cursor = self._tokens.get(token)
            if cursor is not None:
                self._tokens.move_to_end(token)
        if cursor is None:
            raise HTTPException(status_code=400, detail="The page token is invalid or expired.")
        if (
            cursor.route != route
            or cursor.principal != principal
            or cursor.filters_hash != _filters_hash(filters)
            or cursor.sort_key != sort_key
        ):
            raise HTTPException(status_code=400, detail="The page token does not match this request.")
        return cursor

    def _prune(self, now: float) -> None:
        expired = [token for token, cursor in self._tokens.items() if cursor.expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)


page_tokens = PageTokenRegistry()


def page(
    records: Iterable[T],
    *,
    route: str,
    principal: str,
    filters: Mapping[str, Any],
    page_size: int = DEFAULT_PAGE_SIZE,
    page_token: str | None = None,
    sort_key: str,
    key: Callable[[T], Any],
    identity: Callable[[T], Any] | None = None,
    reverse: bool = False,
) -> dict[str, Any]:
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"page_size must be between 1 and {MAX_PAGE_SIZE}.")
    ordered = sorted(records, key=key, reverse=reverse)
    identify = identity or key
    offset = 0
    snapshot = tuple(_identity(identify(item)) for item in ordered)
    if page_token:
        cursor = page_tokens.resolve(
            page_token,
            route=route,
            principal=principal,
            filters=filters,
            sort_key=sort_key,
        )
        offset = cursor.offset
        snapshot = cursor.snapshot
        by_identity = {_identity(identify(item)): item for item in ordered}
        ordered = [by_identity[item_id] for item_id in snapshot if item_id in by_identity]
    items = ordered[offset : offset + page_size]
    next_offset = offset + len(items)
    next_page_token = None
    if next_offset < len(ordered):
        next_page_token = page_tokens.issue(
            route=route,
            principal=principal,
            filters=filters,
            sort_key=sort_key,
            offset=next_offset,
            snapshot=snapshot,
        )
    return {"items": items, "next_page_token": next_page_token}


def _filters_hash(filters: Mapping[str, Any]) -> str:
    payload = json.dumps(filters, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256(payload).hexdigest()


def _identity(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
