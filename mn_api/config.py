from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_BLUEPRINT_REPO = "https://github.com/MirrorNeuronLab/otterdesk-blueprints"
DEV_LOCAL_BLUEPRINT_REPO_ENV = "MN_DEV_LOCAL_BLUEPRINT_REPO"
DEV_LOCAL_BLUEPRINT_REPO_ALIAS_ENV = "DEV_LOCAL_BLUEPRINT_REPO"


def _mn_home() -> Path:
    configured_home = os.getenv("MN_HOME") or os.getenv("MIRROR_NEURON_HOME")
    return Path(configured_home).expanduser() if configured_home else Path.home() / ".mn"


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
    blueprint_repo: str
    configured_blueprint_repo: str
    dev_local_blueprint_repo: str

    @classmethod
    def from_env(cls) -> "ApiConfig":
        env = os.getenv("MN_ENV", "dev")
        timeout = _optional_float("MN_GRPC_TIMEOUT_SECONDS", "10")
        core_host = os.getenv("MN_CORE_HOST", "localhost")
        configured_blueprint_repo = os.getenv(
            "MN_BLUEPRINT_REPO",
            DEFAULT_BLUEPRINT_REPO,
        )
        dev_local_blueprint_repo = _dev_local_blueprint_repo()
        blueprint_repo = (
            dev_local_blueprint_repo
            if env in {"dev", "test"} and dev_local_blueprint_repo
            else configured_blueprint_repo
        )
        config = cls(
            env=env,
            host=os.getenv("MN_API_HOST", "localhost"),
            port=_int("MN_API_PORT", "54001"),
            grpc_target=os.getenv(
                "MN_GRPC_TARGET",
                os.getenv("MN_CORE_GRPC_TARGET", f"{core_host}:55051"),
            ),
            grpc_timeout_seconds=timeout,
            grpc_auth_token=_token_from_env_or_file(
                "MN_GRPC_AUTH_TOKEN",
                _mn_home() / "grpc_auth.token",
                legacy_path=Path.home() / ".mirror_neuron" / "grpc_auth.token",
            ),
            grpc_admin_token=_token_from_env_or_file(
                "MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN",
                _mn_home() / "grpc_admin.token",
                legacy_path=Path.home() / ".mirror_neuron" / "grpc_admin.token",
            ),
            api_token=os.getenv("MN_API_TOKEN", ""),
            request_size_limit_bytes=_int(
                "MN_API_REQUEST_SIZE_LIMIT_BYTES",
                str(5 * 1024 * 1024),
            ),
            cors_allow_origins=_csv(
                os.getenv("MN_API_CORS_ALLOW_ORIGINS", "")
            ),
            blueprint_repo=blueprint_repo,
            configured_blueprint_repo=configured_blueprint_repo,
            dev_local_blueprint_repo=dev_local_blueprint_repo,
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
        if not self.blueprint_repo:
            raise ValueError("MN_BLUEPRINT_REPO must be a non-empty path")
        if self.prod and self.dev_local_blueprint_repo:
            raise ValueError(f"{DEV_LOCAL_BLUEPRINT_REPO_ENV} can only be used when MN_ENV=dev or MN_ENV=test")


def _dev_local_blueprint_repo() -> str:
    return (
        os.getenv(DEV_LOCAL_BLUEPRINT_REPO_ENV, "").strip()
        or os.getenv(DEV_LOCAL_BLUEPRINT_REPO_ALIAS_ENV, "").strip()
    )


def _int(name: str, default: str) -> int:
    value = os.getenv(name, default)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _optional_float(name: str, default: str) -> float | None:
    value = os.getenv(name, default)
    if value.lower() in {"", "0", "none"}:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, 0, or none") from exc


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _token_from_env_or_file(name: str, path: Path, *, legacy_path: Path | None = None) -> str:
    token = os.getenv(name)
    if token:
        return token

    for token_path in (path, legacy_path):
        if token_path is None:
            continue
        try:
            value = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def auth_enabled(config: ApiConfig) -> bool:
    return bool(config.api_token)
