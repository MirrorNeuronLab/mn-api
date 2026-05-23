from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    sdk_path = parent / "mn-python-sdk"
    if (sdk_path / "mn_sdk" / "client.py").exists() and str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))
        break

from mn_sdk import Client

from mn_api.config import ApiConfig
from mn_api.logging_config import configure_logging


config = ApiConfig.from_env()
logger = configure_logging()
BUNDLE_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "mirror_neuron_api_bundles"

_client: Client | None = None


def _client_kwargs() -> dict:
    kwargs = {
        "target": config.grpc_target,
        "timeout": config.grpc_timeout_seconds,
        "auth_token": config.grpc_auth_token,
    }
    try:
        client_params = inspect.signature(Client).parameters
    except (TypeError, ValueError):
        client_params = {}
    if "admin_token" in client_params:
        kwargs["admin_token"] = config.grpc_admin_token
    elif config.grpc_admin_token:
        logger.warning(
            "mn_sdk.Client does not accept admin_token; upgrade mirrorneuron-python-sdk "
            "for destructive admin RPC support."
        )
    return kwargs


def get_client() -> Client:
    global _client
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
