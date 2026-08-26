"""API-specific configuration keys composed with the canonical SDK schema."""

from __future__ import annotations

from pathlib import Path
from mn_sdk.config import (
    SUPPORTED_CONFIG_KEYS as SDK_CONFIG_KEYS,
    ConfigError,
    ConfigKey,
    compose_config_keys,
    is_sensitive_key as sdk_is_sensitive_key,
    parse_bool,
    parse_csv,
    parse_float,
    parse_int,
    parse_path as sdk_parse_path,
    parse_str,
    parse_url as sdk_parse_url,
    redact_config_values as sdk_redact_config_values,
)


parse_string = parse_str
parse_list = parse_csv


def parse_url(name: str, value: str) -> str:
    parsed = sdk_parse_url(name, value)
    if parsed and not parsed.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must be an absolute HTTP(S) URL")
    return parsed


def parse_path(name: str, value: str) -> Path:
    if not str(value).strip():
        raise ConfigError(f"{name} must be a path")
    return sdk_parse_path(name, value)


API_CONFIG_KEYS: tuple[ConfigKey, ...] = (
    ConfigKey("MN_API_LOG_PATH", parse_path, default="", description="API log file path."),
    ConfigKey(
        "MN_API_REQUEST_SIZE_LIMIT_BYTES",
        parse_int,
        default=str(5 * 1024 * 1024),
        description="Maximum HTTP request body size.",
    ),
    ConfigKey(
        "MN_API_CORS_ALLOW_ORIGINS",
        parse_list,
        default="",
        description="Comma-separated CORS origins.",
    ),
    ConfigKey(
        "MN_WEB_UI_API_BASE_URL",
        parse_url,
        default="",
        description="Web UI upstream API URL.",
    ),
    ConfigKey(
        "MN_WEB_UI_DIST_DIR",
        parse_path,
        default="",
        description="Web UI static build directory.",
    ),
    ConfigKey(
        "MN_WEB_UI_PROXY_TIMEOUT_SECONDS",
        parse_float,
        default="30",
        description="Web UI proxy timeout.",
    ),
    ConfigKey("MN_MEMBRANE_PROJECT_PATH", parse_path, default=""),
    ConfigKey("MN_MEMBRANE_SDK_PATH", parse_path, default=""),
    ConfigKey("MN_PROCESS_CLEANUP_TIMEOUT_SECONDS", parse_float, default="5"),
    ConfigKey("OPENSHELL_CONFIG_DIR", parse_path, default="~/.config/openshell"),
    ConfigKey("OPENSHELL_GATEWAY", parse_string, default=""),
)

SUPPORTED_CONFIG_KEYS = compose_config_keys(SDK_CONFIG_KEYS, API_CONFIG_KEYS)
CONFIG_KEY_MAP = {key.name: key for key in SUPPORTED_CONFIG_KEYS}


def is_sensitive_key(name: str) -> bool:
    return sdk_is_sensitive_key(name, CONFIG_KEY_MAP)


def redact_config_values(values: dict[str, str]) -> dict[str, str]:
    return sdk_redact_config_values(values, keys=CONFIG_KEY_MAP)


__all__ = [
    "API_CONFIG_KEYS",
    "CONFIG_KEY_MAP",
    "SUPPORTED_CONFIG_KEYS",
    "ConfigError",
    "ConfigKey",
    "is_sensitive_key",
    "parse_bool",
    "parse_float",
    "parse_int",
    "parse_list",
    "parse_path",
    "parse_string",
    "parse_url",
    "redact_config_values",
]
