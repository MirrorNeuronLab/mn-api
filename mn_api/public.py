from __future__ import annotations

import json
import os
from typing import Any, Iterable

from fastapi.responses import JSONResponse

from mn_api.http_semantics import idempotency_records, strong_etag


_PRIVATE_KEYS = {
    "active_blueprint_location",
    "api_key",
    "blueprint_local",
    "blueprint_repo",
    "bundle_dir",
    "bundle_path",
    "config_path",
    "host_path",
    "litellm_config_path",
    "local_peer_auth_token",
    "join_token",
    "peer_auth_token",
    "repo_dir",
    "run_dir",
    "runs_root",
    "secret",
    "token",
}


def public_value(value: Any) -> Any:
    """Remove transport-version fields, secrets, and absolute host paths."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key == "version" or normalized_key.startswith("_") or normalized_key in _PRIVATE_KEYS:
                continue
            if isinstance(item, str) and os.path.isabs(item) and (normalized_key == "path" or normalized_key.endswith("_path")):
                continue
            result[normalized_key] = public_value(item)
        return result
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def decode(value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else value
    return value


def records(value: Any, *keys: str) -> list[dict[str, Any]]:
    decoded = decode(value)
    if isinstance(decoded, list):
        return [public_value(item) for item in decoded if isinstance(item, dict)]
    if isinstance(decoded, dict):
        for key in keys:
            candidate = decoded.get(key)
            if isinstance(candidate, list):
                return [public_value(item) for item in candidate if isinstance(item, dict)]
    return []


def resource_response(
    resource: Any,
    *,
    status_code: int = 200,
    location: str | None = None,
    etag: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = public_value(decode(resource))
    headers = dict(extra_headers or {})
    if location:
        headers["Location"] = location
    if etag:
        headers["ETag"] = strong_etag(body)
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def idempotent_response(
    *,
    principal: str,
    route: str,
    key: str | None,
    body: Any,
    call,
    status_code: int,
    location,
) -> JSONResponse:
    fingerprint = idempotency_records.fingerprint(body)
    if key:
        replay = idempotency_records.find(
            principal=principal,
            route=route,
            key=key,
            fingerprint=fingerprint,
        )
        if replay is not None:
            headers = {**dict(replay.headers), "Idempotency-Replayed": "true"}
            return JSONResponse(status_code=replay.status_code, content=replay.body, headers=headers)

    raw_result = call()
    if isinstance(raw_result, JSONResponse):
        return raw_result
    result = public_value(decode(raw_result))
    resolved_location = location(result) if callable(location) else location
    headers = {"Location": resolved_location} if resolved_location else {}
    if key:
        idempotency_records.store(
            principal=principal,
            route=route,
            key=key,
            fingerprint=fingerprint,
            status_code=status_code,
            headers=headers,
            body=result,
        )
    return JSONResponse(status_code=status_code, content=result, headers=headers)


def first_identifier(resource: Any, names: Iterable[str]) -> str:
    if not isinstance(resource, dict):
        return ""
    for name in names:
        value = resource.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return ""
