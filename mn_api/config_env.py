from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


ENV_ALIASES = {
    "development": "dev",
    "prod": "prod",
    "production": "prod",
}


@dataclass(frozen=True)
class ConfigSource:
    mn_env: str
    env_file: Path
    profile_env_file: Path
    env_file_values: dict[str, str]
    profile_env_file_values: dict[str, str]
    real_env: dict[str, str]
    effective_env: dict[str, str]

    @property
    def loaded_files(self) -> tuple[Path, ...]:
        files: list[Path] = []
        if self.env_file_values:
            files.append(self.env_file)
        if self.profile_env_file_values:
            files.append(self.profile_env_file)
        return tuple(files)


def load_config_source(
    *,
    env: Mapping[str, str] | None = None,
    env_dir: str | Path | None = None,
) -> ConfigSource:
    real_env = {str(key): str(value) for key, value in (env if env is not None else os.environ).items()}
    mn_env = normalize_mn_env(real_env.get("MN_ENV") or "dev")
    root = Path(env_dir).expanduser() if env_dir is not None else Path.cwd()
    env_file = root / ".env"
    profile_env_file = root / f".env.{profile_name(mn_env)}"
    base_values = read_dotenv(env_file)
    profile_values = read_dotenv(profile_env_file)
    effective = merge_env_layers(base_values, profile_values, real_env)
    effective["MN_ENV"] = real_env.get("MN_ENV") or mn_env
    return ConfigSource(
        mn_env=mn_env,
        env_file=env_file,
        profile_env_file=profile_env_file,
        env_file_values=base_values,
        profile_env_file_values=profile_values,
        real_env=real_env,
        effective_env=effective,
    )


def normalize_mn_env(value: str) -> str:
    normalized = value.strip().lower()
    return ENV_ALIASES.get(normalized, normalized or "dev")


def profile_name(mn_env: str) -> str:
    return "prod" if normalize_mn_env(mn_env) == "prod" else normalize_mn_env(mn_env)


def merge_env_layers(*layers: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for layer in layers:
        for key, value in layer.items():
            merged[str(key)] = str(value)
    return merged


def read_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        parsed = parse_dotenv_line(line)
        if parsed is not None:
            key, value = parsed
            values[key] = value
    return values


def parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        return None
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return key, _strip_inline_comment(_unquote(raw_value.strip()))


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_inline_comment(value: str) -> str:
    if not value or value[0] in {"'", '"'}:
        return value
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value
