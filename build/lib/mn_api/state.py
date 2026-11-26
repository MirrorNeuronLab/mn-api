from __future__ import annotations

import sys
import tempfile
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    sdk_path = parent / "mn-python-sdk"
    if (sdk_path / "mn_sdk" / "client.py").exists() and str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))
        break

from mn_sdk import Client
from mn_sdk.runtime_client import grpc_client_settings, runtime_client_kwargs

from mn_api.config import ApiConfig
from mn_api.logging_config import configure_logging


config = ApiConfig.from_env()
logger = configure_logging()
BUNDLE_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "mirror_neuron_api_bundles"

_client: Client | None = None


def _grpc_client_settings(config_obj) -> tuple:
    return grpc_client_settings(config_obj)


def refresh_config_from_env():
    global config
    if not isinstance(config, ApiConfig):
        return config
    try:
        refreshed = ApiConfig.from_env()
    except Exception:
        logger.exception("Failed to refresh mn-api runtime configuration")
        return config
    if _client is not None and _grpc_client_settings(refreshed) != _grpc_client_settings(config):
        close_client()
    config = refreshed
    return config


def _client_kwargs() -> dict:
    current_config = refresh_config_from_env()
    return runtime_client_kwargs(current_config, client_cls=Client, warn=logger.warning)


def get_client() -> Client:
    global _client
    refresh_config_from_env()
    if _client is None:
        _client = Client(**_client_kwargs())
    return _client


def close_client() -> None:
    global _client
    current = _client
    _client = None
    channel = getattr(current, "channel", None)
    close = getattr(channel, "close", None)
    if callable(close):
        close()


class ClientProxy:
    def __getattr__(self, name: str):
        return getattr(get_client(), name)


client = ClientProxy()
