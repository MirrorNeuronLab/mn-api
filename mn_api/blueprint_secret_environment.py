from __future__ import annotations

import json
import re
from typing import Any, Mapping

from fastapi import HTTPException
from mn_sdk.blueprints.secrets import executable_manifest_nodes
from mn_sdk.blueprints.secrets import inject_declared_secret_environment as _inject_secret_environment


SECRET_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
MAX_SECRET_ENVIRONMENT_VALUES = 16
MAX_SECRET_ENVIRONMENT_VALUE_BYTES = 8192


def requested_secret_environment(requested: Mapping[str, Any] | None) -> dict[str, str]:
    values = requested or {}
    if len(values) > MAX_SECRET_ENVIRONMENT_VALUES:
        raise HTTPException(status_code=422, detail="Too many secret environment values were supplied.")
    environment: dict[str, str] = {}
    for raw_name, secret in values.items():
        name = str(raw_name or "").strip()
        if not SECRET_ENV_NAME_PATTERN.fullmatch(name):
            raise HTTPException(status_code=422, detail="A secret environment name is invalid.")
        value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        if not value:
            raise HTTPException(status_code=422, detail=f"Secret environment value {name} is empty.")
        if len(value.encode("utf-8")) > MAX_SECRET_ENVIRONMENT_VALUE_BYTES:
            raise HTTPException(status_code=422, detail=f"Secret environment value {name} is too large.")
        environment[name] = value
    return environment


def declared_pass_environment_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        pass_env = value.get("pass_env")
        if isinstance(pass_env, list):
            names.update(str(name).strip() for name in pass_env if SECRET_ENV_NAME_PATTERN.fullmatch(str(name).strip()))
        for nested in value.values():
            names.update(declared_pass_environment_names(nested))
    elif isinstance(value, list):
        for nested in value:
            names.update(declared_pass_environment_names(nested))
    return names


def validate_blueprint_secret_environment(manifest: Mapping[str, Any], environment: Mapping[str, str]) -> None:
    if not environment:
        return
    undeclared = sorted(set(environment) - declared_pass_environment_names(manifest))
    if undeclared:
        joined = ", ".join(undeclared)
        raise HTTPException(
            status_code=422,
            detail=f"Secret environment values are not declared by this blueprint: {joined}.",
        )




def inject_declared_secret_environment(manifest_json: str, environment: Mapping[str, str]) -> str:
    try:
        return _inject_secret_environment(manifest_json, environment)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def manifest_without_secret_environment(manifest_json: str, environment: Mapping[str, str]) -> dict[str, Any]:
    manifest = json.loads(manifest_json)
    for node in executable_manifest_nodes(manifest):
        config = node.get("config")
        node_environment = config.get("environment") if isinstance(config, dict) else None
        if not isinstance(node_environment, dict):
            continue
        for name in environment:
            node_environment.pop(name, None)
    return manifest
