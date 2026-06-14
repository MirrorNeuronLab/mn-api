from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    sdk_path = parent / "mn-python-sdk"
    if (sdk_path / "mn_sdk" / "runtime_config.py").exists() and str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))
        break

from mn_sdk.blueprint_source import resolve_blueprint_source_config
from mn_sdk.runtime_config import RuntimeConfig


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

    @classmethod
    def from_env(cls) -> "ApiConfig":
        runtime_config = RuntimeConfig.from_env()
        runtime_env = runtime_config.runtime_env
        env = _env_value("MN_ENV", runtime_env, "dev")
        blueprint_source = resolve_blueprint_source_config(runtime_env=runtime_env)
        config = cls(
            env=env,
            host=os.getenv("MN_API_HOST") or runtime_env.get("MN_API_HOST") or "localhost",
            port=_int_value(os.getenv("MN_API_PORT") or runtime_env.get("MN_API_PORT") or "54001", "MN_API_PORT"),
            grpc_target=runtime_config.grpc_target,
            grpc_timeout_seconds=runtime_config.grpc_timeout_seconds,
            grpc_auth_token=runtime_config.grpc_auth_token,
            grpc_admin_token=runtime_config.grpc_admin_token,
            api_token=os.getenv("MN_API_TOKEN", ""),
            request_size_limit_bytes=_int(
                "MN_API_REQUEST_SIZE_LIMIT_BYTES",
                str(5 * 1024 * 1024),
            ),
            cors_allow_origins=_csv(
                os.getenv("MN_API_CORS_ALLOW_ORIGINS", "")
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
            raise ValueError("MN_ENV must be one of dev, test, or prod")
        if not 1 <= self.port <= 65535:
            raise ValueError("MN_API_PORT must be between 1 and 65535")
        if self.request_size_limit_bytes <= 0:
            raise ValueError("MN_API_REQUEST_SIZE_LIMIT_BYTES must be > 0")
        if self.prod and not self.api_token:
            raise ValueError("MN_API_TOKEN is required when MN_ENV=prod")
        if self.blueprint_source not in {"github", "local"}:
            raise ValueError("MN_BLUEPRINT_SOURCE must be one of github or local")
        if self.blueprint_source == "github" and not self.blueprint_repo:
            raise ValueError("MN_BLUEPRINT_REPO is required when MN_BLUEPRINT_SOURCE=github")
        if self.blueprint_source == "local" and not self.blueprint_local:
            raise ValueError("MN_BLUEPRINT_LOCAL is required when MN_BLUEPRINT_SOURCE=local")


def _env_value(name: str, runtime_env: dict[str, str], default: str = "") -> str:
    return (os.getenv(name, "").strip() or str(runtime_env.get(name) or "").strip() or default)


def _int(name: str, default: str) -> int:
    value = os.getenv(name, default)
    return _int_value(value, name)


def _int_value(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def runtime_env_values() -> dict[str, str]:
    return RuntimeConfig.from_env().runtime_env


def auth_enabled(config: ApiConfig) -> bool:
    return bool(config.api_token)
