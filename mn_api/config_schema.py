from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


class ConfigError(ValueError):
    pass


Parser = Callable[[str, str], Any]


@dataclass(frozen=True)
class ConfigKey:
    name: str
    parser: Parser
    default: str | None = None
    required: bool = False
    sensitive: bool = False
    description: str = ""


def parse_string(name: str, value: str) -> str:
    return value.strip()


def parse_int(name: str, value: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def parse_float(name: str, value: str) -> float:
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


def parse_list(name: str, value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_url(name: str, value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute HTTP(S) URL")
    return text


def parse_path(name: str, value: str) -> Path:
    text = value.strip()
    if not text:
        raise ConfigError(f"{name} must be a path")
    return Path(text).expanduser()


SUPPORTED_CONFIG_KEYS: tuple[ConfigKey, ...] = (
    ConfigKey("MN_ENV", parse_string, default="dev", description="Runtime environment: dev, test, or production."),
    ConfigKey("MN_HOME", parse_path, default="~/.mn", description="MirrorNeuron state directory."),
    ConfigKey("MN_LOG_LEVEL", parse_string, default="INFO", description="Python logging level."),
    ConfigKey("MN_LOGS_ROOT", parse_path, default="~/.mn/logs", description="Default log directory."),
    ConfigKey("MN_API_LOG_PATH", parse_path, default="", description="API log file path."),
    ConfigKey("MN_LOG_MAX_BYTES", parse_int, default="1048576", description="Rotating log max file size."),
    ConfigKey("MN_LOG_BACKUP_COUNT", parse_int, default="5", description="Rotating log backup count."),
    ConfigKey("MN_API_HOST", parse_string, default="localhost", description="API bind host."),
    ConfigKey("MN_API_PORT", parse_int, default="54001", description="API bind port."),
    ConfigKey("MN_API_BASE_URL", parse_url, default="", description="External API base URL."),
    ConfigKey("MN_API_TOKEN", parse_string, default="", sensitive=True, description="Bearer token for API auth."),
    ConfigKey(
        "MN_API_REQUEST_SIZE_LIMIT_BYTES",
        parse_int,
        default=str(5 * 1024 * 1024),
        description="Maximum HTTP request body size.",
    ),
    ConfigKey("MN_API_CORS_ALLOW_ORIGINS", parse_list, default="", description="Comma-separated CORS origins."),
    ConfigKey("MN_GRPC_TARGET", parse_string, default="", description="Runtime gRPC target."),
    ConfigKey("MN_GRPC_TIMEOUT_SECONDS", parse_float, default="10", description="Runtime gRPC timeout."),
    ConfigKey("MN_GRPC_AUTH_TOKEN", parse_string, default="", sensitive=True, description="Runtime gRPC auth token."),
    ConfigKey("MN_GRPC_ADMIN_TOKEN", parse_string, default="", sensitive=True, description="Runtime gRPC admin token."),
    ConfigKey("MN_BLUEPRINT_SOURCE", parse_string, default="github", description="Blueprint source: github or local."),
    ConfigKey("MN_BLUEPRINT_REPO", parse_string, default="", description="Blueprint Git repository URL."),
    ConfigKey("MN_BLUEPRINT_LOCAL", parse_path, default="", description="Local blueprint catalog path."),
    ConfigKey("MN_BLUEPRINT_REPO_CACHE", parse_path, default="~/.cache/mirror-neuron/blueprint-repos"),
    ConfigKey("MN_WEB_UI_HOST", parse_string, default="localhost", description="Web UI bind host."),
    ConfigKey("MN_WEB_UI_PORT", parse_int, default="55173", description="Web UI bind port."),
    ConfigKey("MN_WEB_UI_API_BASE_URL", parse_url, default="", description="Web UI upstream API URL."),
    ConfigKey("MN_WEB_UI_DIST_DIR", parse_path, default="", description="Web UI static build directory."),
    ConfigKey("MN_WEB_UI_PROXY_TIMEOUT_SECONDS", parse_float, default="30", description="Web UI proxy timeout."),
    ConfigKey("MN_WORKSPACE_ROOT", parse_path, default="", description="MirrorNeuron workspace root."),
    ConfigKey("MN_MEMBRANE_PROJECT_PATH", parse_path, default="", description="Membrane project path."),
    ConfigKey("MN_MEMBRANE_SDK_PATH", parse_path, default="", description="Membrane SDK path."),
    ConfigKey("MN_SKILLS_ROOT", parse_path, default="", description="Runtime skills root."),
    ConfigKey("MN_PRE_LAUNCH_TIMEOUT_SECONDS", parse_float, default="30"),
    ConfigKey("MN_POST_LAUNCH_TIMEOUT_SECONDS", parse_float, default="10"),
    ConfigKey("MN_PROCESS_CLEANUP_TIMEOUT_SECONDS", parse_float, default="5"),
    ConfigKey("MN_RUN_BACKGROUND_EVENT_RELAY", parse_bool, default="1"),
    ConfigKey("MN_RUN_EVENT_RELAY_POLL_SECONDS", parse_float, default=""),
    ConfigKey("MN_RUN_EVENT_RELAY_MAX_SECONDS", parse_float, default=""),
    ConfigKey("OPENSHELL_CONFIG_DIR", parse_path, default="~/.config/openshell"),
    ConfigKey("OPENSHELL_GATEWAY", parse_string, default=""),
)

CONFIG_KEY_MAP = {key.name: key for key in SUPPORTED_CONFIG_KEYS}


def is_sensitive_key(name: str) -> bool:
    key = CONFIG_KEY_MAP.get(name)
    upper_name = name.upper()
    return bool(key and key.sensitive) or any(marker in upper_name for marker in ("TOKEN", "SECRET", "PASSWORD", "KEY"))


def redact_config_values(values: dict[str, str]) -> dict[str, str]:
    return {key: ("<redacted>" if is_sensitive_key(key) and value else value) for key, value in values.items()}
