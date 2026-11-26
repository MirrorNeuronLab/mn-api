from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

for parent in Path(__file__).resolve().parents:
    sdk_path = parent / "mn-python-sdk"
    if (sdk_path / "mn_sdk" / "runtime_config.py").exists() and str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))
        break

from mn_sdk.blueprints.limits import MAX_PACKAGE_BYTES
from mn_sdk.blueprint_source import resolve_blueprint_source_config
from mn_sdk.runtime_config import RuntimeConfig
from mn_sdk.config.types import MISSING

from mn_api.config_env import ConfigSource, load_config_source, normalize_mn_env
from mn_api.config_schema import CONFIG_KEY_MAP, ConfigError, is_sensitive_key, redact_config_values
from mn_api.path_utils import default_logs_root


@dataclass(frozen=True)
class ApiConfig:
    env: str
    host: str
    port: int
    grpc_target: str
    grpc_timeout_seconds: float | None
    grpc_auth_token: str
    grpc_admin_token: str
    api_token: str
    request_size_limit_bytes: int
    cors_allow_origins: list[str]
    blueprint_source: str
    blueprint_repo: str
    blueprint_local: str
    active_blueprint_location: str
    blueprint_upload_limit_bytes: int = MAX_PACKAGE_BYTES

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        env_dir: str | Path | None = None,
    ) -> "ApiConfig":
        source = load_config_source(env=env, env_dir=env_dir)
        try:
            runtime_config = RuntimeConfig.from_env(env=source.effective_env, env_dir=env_dir)
        except Exception as exc:
            if exc.__class__.__name__ == "ConfigError":
                raise ConfigError(str(exc)) from exc
            raise
        runtime_env = runtime_config.runtime_env
        mn_env = normalize_mn_env(_first_string(source.effective_env.get("MN_ENV"), runtime_env.get("MN_ENV"), "dev"))
        blueprint_source = resolve_blueprint_source_config(
            env=source.effective_env,
            runtime_env=runtime_env,
            env_dir=env_dir,
        )
        config = cls(
            env=mn_env,
            host=config_string("MN_API_HOST", source=source, runtime_env=runtime_env, default="localhost"),
            port=config_int("MN_API_PORT", source=source, runtime_env=runtime_env, default=54001),
            grpc_target=runtime_config.grpc_target,
            grpc_timeout_seconds=runtime_config.grpc_timeout_seconds,
            grpc_auth_token=runtime_config.grpc_auth_token,
            grpc_admin_token=runtime_config.grpc_admin_token,
            api_token=config_string("MN_API_TOKEN", source=source, runtime_env=runtime_env, default=""),
            request_size_limit_bytes=config_int(
                "MN_API_REQUEST_SIZE_LIMIT_BYTES",
                source=source,
                runtime_env=runtime_env,
                default=5 * 1024 * 1024,
            ),
            blueprint_upload_limit_bytes=config_int(
                "MN_API_BLUEPRINT_UPLOAD_LIMIT_BYTES",
                source=source,
                runtime_env=runtime_env,
                default=MAX_PACKAGE_BYTES,
            ),
            cors_allow_origins=config_list(
                "MN_API_CORS_ALLOW_ORIGINS",
                source=source,
                runtime_env=runtime_env,
                default=[],
            ),
            blueprint_source=blueprint_source.source,
            blueprint_repo=blueprint_source.repo,
            blueprint_local=blueprint_source.local,
            active_blueprint_location=blueprint_source.active_location,
        )
        config.validate()
        return config

    @property
    def prod(self) -> bool:
        return self.env == "prod"

    def validate(self) -> None:
        if self.env not in {"dev", "test", "prod"}:
            raise ConfigError("MN_ENV must be one of dev, development, test, prod, or production")
        if not 1 <= self.port <= 65535:
            raise ConfigError("MN_API_PORT must be between 1 and 65535")
        if self.request_size_limit_bytes <= 0:
            raise ConfigError("MN_API_REQUEST_SIZE_LIMIT_BYTES must be > 0")
        if not 0 < self.blueprint_upload_limit_bytes <= MAX_PACKAGE_BYTES:
            raise ConfigError(f"MN_API_BLUEPRINT_UPLOAD_LIMIT_BYTES must be between 1 and {MAX_PACKAGE_BYTES}")
        if self.prod and not self.api_token:
            raise ConfigError("MN_API_TOKEN is required when MN_ENV=prod or MN_ENV=production")
        if self.blueprint_source not in {"github", "local"}:
            raise ConfigError("MN_BLUEPRINT_SOURCE must be one of github or local")
        if self.blueprint_source == "github" and not self.blueprint_repo:
            raise ConfigError("MN_BLUEPRINT_REPO is required when MN_BLUEPRINT_SOURCE=github")
        if self.blueprint_source == "local" and not self.blueprint_local:
            raise ConfigError("MN_BLUEPRINT_LOCAL is required when MN_BLUEPRINT_SOURCE=local")


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    api_log_path: Path
    max_bytes: int
    backup_count: int

    @classmethod
    def from_env(cls, *, env: Mapping[str, str] | None = None, env_dir: str | Path | None = None) -> "LoggingConfig":
        source = load_config_source(env=env, env_dir=env_dir)
        runtime_env = RuntimeConfig.from_env(env=source.effective_env, env_dir=env_dir).runtime_env
        default_log_path = default_logs_root(env=source.effective_env) / "api.log"
        return cls(
            level=config_string("MN_LOG_LEVEL", source=source, runtime_env=runtime_env, default="INFO").upper(),
            api_log_path=config_path(
                "MN_API_LOG_PATH",
                source=source,
                runtime_env=runtime_env,
                default=default_log_path,
                allow_empty=True,
            )
            or default_log_path,
            max_bytes=config_int("MN_LOG_MAX_BYTES", source=source, runtime_env=runtime_env, default=1048576),
            backup_count=config_int("MN_LOG_BACKUP_COUNT", source=source, runtime_env=runtime_env, default=5),
        )


@dataclass(frozen=True)
class WebUiConfig:
    host: str
    port: int
    api_base_url: str
    api_token: str
    dist_dir: Path | None
    proxy_timeout_seconds: float

    @classmethod
    def from_env(cls, *, env: Mapping[str, str] | None = None, env_dir: str | Path | None = None) -> "WebUiConfig":
        source = load_config_source(env=env, env_dir=env_dir)
        runtime_config = RuntimeConfig.from_env(env=source.effective_env, env_dir=env_dir)
        runtime_env = runtime_config.runtime_env
        api_base = config_string("MN_WEB_UI_API_BASE_URL", source=source, runtime_env=runtime_env, default="")
        if not api_base:
            api_base = config_string("MN_API_BASE_URL", source=source, runtime_env=runtime_env, default="")
        host = config_string("MN_API_HOST", source=source, runtime_env=runtime_env, default="localhost")
        port = config_int("MN_API_PORT", source=source, runtime_env=runtime_env, default=54001)
        api_token = config_string("MN_API_TOKEN", source=source, runtime_env=runtime_env, default="")
        if not api_token:
            api_token = _read_token_file(runtime_config.mn_home / "api.token")
        dist_dir = config_path(
            "MN_WEB_UI_DIST_DIR",
            source=source,
            runtime_env=runtime_env,
            default=None,
            allow_empty=True,
        )
        return cls(
            host=config_string("MN_WEB_UI_HOST", source=source, runtime_env=runtime_env, default="localhost"),
            port=config_int("MN_WEB_UI_PORT", source=source, runtime_env=runtime_env, default=55173),
            api_base_url=(api_base or f"http://{host}:{port}/api/v1").rstrip("/"),
            api_token=api_token,
            dist_dir=dist_dir,
            proxy_timeout_seconds=config_float(
                "MN_WEB_UI_PROXY_TIMEOUT_SECONDS",
                source=source,
                runtime_env=runtime_env,
                default=30.0,
            ),
        )


def config_string(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
    default: str = "",
) -> str:
    value = _raw_config_value(name, source=source, runtime_env=runtime_env, default=default)
    return str(_parse_config_value(name, value))


def config_int(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
    default: int | str = 0,
) -> int:
    value = _raw_config_value(name, source=source, runtime_env=runtime_env, default=str(default))
    return int(_parse_config_value(name, value))


def config_float(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
    default: float | str = 0.0,
) -> float:
    value = _raw_config_value(name, source=source, runtime_env=runtime_env, default=str(default))
    return float(_parse_config_value(name, value))


def config_bool(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
    default: bool | str = False,
) -> bool:
    raw_default = default if isinstance(default, str) else ("1" if default else "0")
    value = _raw_config_value(name, source=source, runtime_env=runtime_env, default=raw_default)
    return bool(_parse_config_value(name, value))


def config_list(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
    default: list[str] | str | None = None,
) -> list[str]:
    raw_default = ",".join(default) if isinstance(default, list) else (default or "")
    value = _raw_config_value(name, source=source, runtime_env=runtime_env, default=raw_default)
    parsed = _parse_config_value(name, value)
    return parsed if isinstance(parsed, list) else []


def config_path(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
    default: str | Path | None = None,
    allow_empty: bool = False,
) -> Path | None:
    raw_default = "" if default is None else str(default)
    value = _raw_config_value(name, source=source, runtime_env=runtime_env, default=raw_default)
    if allow_empty and not str(value).strip():
        return None
    parsed = _parse_config_value(name, value)
    return parsed if isinstance(parsed, Path) else Path(str(parsed)).expanduser()


def config_value(
    name: str,
    default: str = "",
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
) -> str:
    return _raw_config_value(name, source=source, runtime_env=runtime_env, default=default)


def config_optional_value(
    name: str,
    *,
    source: ConfigSource | None = None,
    runtime_env: Mapping[str, str] | None = None,
) -> str | None:
    config_source = source or load_config_source()
    if name in config_source.effective_env:
        return str(config_source.effective_env[name]).strip() or None
    if runtime_env is not None and name in runtime_env:
        return str(runtime_env[name]).strip() or None
    return None


def subprocess_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(load_config_source().effective_env)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def safe_config_values(*, env: Mapping[str, str] | None = None, env_dir: str | Path | None = None) -> dict[str, str]:
    return redact_config_values(load_config_source(env=env, env_dir=env_dir).effective_env)


def _parse_config_value(name: str, value: str) -> Any:
    key = CONFIG_KEY_MAP.get(name)
    if key is None:
        return value.strip()
    if not value.strip():
        if key.required:
            raise ConfigError(f"{name} is required")
        return "" if key.default is MISSING or key.default in {None, ""} else key.parser(name, str(key.default))
    return key.parser(name, value)


def _raw_config_value(
    name: str,
    *,
    source: ConfigSource | None,
    runtime_env: Mapping[str, str] | None,
    default: str,
) -> str:
    config_source = source or load_config_source()
    if name in config_source.effective_env:
        return str(config_source.effective_env[name]).strip()
    if runtime_env is not None and name in runtime_env:
        return str(runtime_env[name]).strip()
    key = CONFIG_KEY_MAP.get(name)
    if key and key.default is not MISSING and key.default not in {None, ""}:
        return str(key.default or "")
    return default


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_token_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def runtime_env_values() -> dict[str, str]:
    source = load_config_source()
    return RuntimeConfig.from_env(env=source.effective_env).runtime_env


def effective_env_values() -> dict[str, str]:
    return dict(load_config_source().effective_env)


def auth_enabled(config: ApiConfig) -> bool:
    return bool(config.api_token)


def redacted_value(name: str, value: str) -> str:
    return "<redacted>" if is_sensitive_key(name) and value else value
