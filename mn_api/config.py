from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_BLUEPRINT_REPO = "https://github.com/MirrorNeuronLab/mn-blueprints.git"
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
        runtime_env = _read_env_file(_mn_home() / "docker-compose.env")
        env = os.getenv("MN_ENV", "dev")
        timeout = _optional_float("MN_GRPC_TIMEOUT_SECONDS", "10")
        core_host = os.getenv("MN_CORE_HOST") or runtime_env.get("MN_CORE_HOST") or "localhost"
        grpc_port = os.getenv("MN_GRPC_PORT") or runtime_env.get("MN_GRPC_PORT") or "55051"
        configured_blueprint_repo = _env_value("MN_BLUEPRINT_REPO", runtime_env, _default_blueprint_repo(runtime_env))
        dev_local_blueprint_repo = _dev_local_blueprint_repo(runtime_env)
        blueprint_repo = (
            dev_local_blueprint_repo
            if env in {"dev", "test"} and dev_local_blueprint_repo
            else configured_blueprint_repo
        )
        config = cls(
            env=env,
            host=os.getenv("MN_API_HOST") or runtime_env.get("MN_API_HOST") or "localhost",
            port=_int_value(os.getenv("MN_API_PORT") or runtime_env.get("MN_API_PORT") or "54001", "MN_API_PORT"),
            grpc_target=os.getenv(
                "MN_GRPC_TARGET",
                os.getenv(
                    "MN_CORE_GRPC_TARGET",
                    runtime_env.get("MN_GRPC_TARGET")
                    or runtime_env.get("MN_CORE_GRPC_TARGET")
                    or f"{core_host}:{grpc_port}",
                ),
            ),
            grpc_timeout_seconds=timeout,
            grpc_auth_token=_token_from_env_or_file(
                "MN_GRPC_AUTH_TOKEN",
                _mn_home() / "grpc_auth.token",
                legacy_path=Path.home() / ".mirror_neuron" / "grpc_auth.token",
                runtime_env=runtime_env,
            ),
            grpc_admin_token=_token_from_env_or_file(
                "MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN",
                _mn_home() / "grpc_admin.token",
                legacy_path=Path.home() / ".mirror_neuron" / "grpc_admin.token",
                runtime_env=runtime_env,
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


def _env_value(name: str, runtime_env: dict[str, str], default: str = "") -> str:
    return (os.getenv(name, "").strip() or str(runtime_env.get(name) or "").strip() or default)


def _default_blueprint_repo(runtime_env: dict[str, str]) -> str:
    return _env_value("MN_DEFAULT_BLUEPRINT_REPO", runtime_env, DEFAULT_BLUEPRINT_REPO)


def _dev_local_blueprint_repo(runtime_env: dict[str, str] | None = None) -> str:
    runtime_env = runtime_env or {}
    return (
        os.getenv(DEV_LOCAL_BLUEPRINT_REPO_ENV, "").strip()
        or os.getenv(DEV_LOCAL_BLUEPRINT_REPO_ALIAS_ENV, "").strip()
        or str(runtime_env.get(DEV_LOCAL_BLUEPRINT_REPO_ENV) or "").strip()
        or str(runtime_env.get(DEV_LOCAL_BLUEPRINT_REPO_ALIAS_ENV) or "").strip()
    )


def _int(name: str, default: str) -> int:
    value = os.getenv(name, default)
    return _int_value(value, name)


def _int_value(value: str, name: str) -> int:
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


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def _token_from_env_or_file(
    name: str,
    path: Path,
    *,
    legacy_path: Path | None = None,
    runtime_env: dict[str, str] | None = None,
) -> str:
    token = os.getenv(name)
    if token:
        return token
    token = (runtime_env or {}).get(name, "")
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
