from __future__ import annotations

import tempfile
from pathlib import Path

from mn_sdk import Client

from mn_api.config import ApiConfig
from mn_api.logging_config import configure_logging


config = ApiConfig.from_env()
logger = configure_logging()
client = Client(
    target=config.grpc_target,
    timeout=config.grpc_timeout_seconds,
    auth_token=config.grpc_auth_token,
)
BUNDLE_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "mirror_neuron_api_bundles"
