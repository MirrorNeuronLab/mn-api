"""Compatibility facade for the canonical SDK dotenv loader."""

from __future__ import annotations

from mn_sdk.config import (
    ConfigSource,
    load_config_source,
    merge_env_layers,
    normalize_mn_env,
    parse_dotenv_line,
    profile_name,
    read_dotenv,
)

__all__ = [
    "ConfigSource",
    "load_config_source",
    "merge_env_layers",
    "normalize_mn_env",
    "parse_dotenv_line",
    "profile_name",
    "read_dotenv",
]
