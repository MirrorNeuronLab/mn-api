from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiConfig:
    env: str
    host: str
    port: int
    grpc_target: str
    grpc_timeout_seconds: float | None
    api_token: str
    request_size_limit_bytes: int
    cors_allow_origins: list[str]

    @classmethod
    def from_env(cls) -> "ApiConfig":
        env = os.getenv("MIRROR_NEURON_ENV", "dev")
        timeout = _optional_float("MIRROR_NEURON_GRPC_TIMEOUT_SECONDS", "10")
        config = cls(
            env=env,
            host=os.getenv("MIRROR_NEURON_API_HOST", "0.0.0.0"),
            port=_int("MIRROR_NEURON_API_PORT", "4001"),
            grpc_target=os.getenv(
                "MIRROR_NEURON_GRPC_TARGET",
                os.getenv("MIRROR_NEURON_CORE_GRPC_TARGET", "localhost:50051"),
            ),
            grpc_timeout_seconds=timeout,
            api_token=os.getenv("MIRROR_NEURON_API_TOKEN", ""),
            request_size_limit_bytes=_int(
                "MIRROR_NEURON_API_REQUEST_SIZE_LIMIT_BYTES",
                str(5 * 1024 * 1024),
            ),
            cors_allow_origins=_csv(
                os.getenv("MIRROR_NEURON_API_CORS_ALLOW_ORIGINS", "")
            ),
        )
        config.validate()
        return config

    @property
    def prod(self) -> bool:
        return self.env == "prod"

    def validate(self) -> None:
        if self.env not in {"dev", "test", "prod"}:
            raise ValueError("MIRROR_NEURON_ENV must be one of dev, test, or prod")
        if not 1 <= self.port <= 65535:
            raise ValueError("MIRROR_NEURON_API_PORT must be between 1 and 65535")
        if self.request_size_limit_bytes <= 0:
            raise ValueError("MIRROR_NEURON_API_REQUEST_SIZE_LIMIT_BYTES must be > 0")
        if self.prod and not self.api_token:
            raise ValueError("MIRROR_NEURON_API_TOKEN is required when MIRROR_NEURON_ENV=prod")


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


def auth_enabled(config: ApiConfig) -> bool:
    return bool(config.api_token)
