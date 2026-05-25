from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mn_api import state
from mn_api.config import ApiConfig


class TestStateClient(unittest.TestCase):
    def setUp(self):
        self.original_config = state.config
        self.original_client = state._client
        self.original_client_class = state.Client
        state._client = None
        state.config = SimpleNamespace(
            grpc_target="localhost:55051",
            grpc_timeout_seconds=10.0,
            grpc_auth_token="",
            grpc_admin_token="admin-secret",
        )

    def tearDown(self):
        state.config = self.original_config
        state._client = self.original_client
        state.Client = self.original_client_class

    def test_get_client_omits_admin_token_for_older_sdk(self):
        calls = []

        class OldClient:
            def __init__(self, target=None, timeout=None, auth_token=None):
                calls.append(
                    {
                        "target": target,
                        "timeout": timeout,
                        "auth_token": auth_token,
                    }
                )

        state.Client = OldClient

        state.get_client()

        self.assertEqual(
            calls,
            [
                {
                    "target": "localhost:55051",
                    "timeout": 10.0,
                    "auth_token": "",
                }
            ],
        )

    def test_get_client_passes_admin_token_for_current_sdk(self):
        calls = []

        class CurrentClient:
            def __init__(
                self,
                target=None,
                timeout=None,
                auth_token=None,
                admin_token=None,
            ):
                calls.append(
                    {
                        "target": target,
                        "timeout": timeout,
                        "auth_token": auth_token,
                        "admin_token": admin_token,
                    }
                )

        state.Client = CurrentClient

        state.get_client()

        self.assertEqual(
            calls,
            [
                {
                    "target": "localhost:55051",
                    "timeout": 10.0,
                    "auth_token": "",
                    "admin_token": "admin-secret",
                }
            ],
        )

    def test_get_client_refreshes_grpc_tokens_from_runtime_env(self):
        calls = []

        state.config = ApiConfig(
            env="dev",
            host="localhost",
            port=54001,
            grpc_target="localhost:55051",
            grpc_timeout_seconds=10.0,
            grpc_auth_token="",
            grpc_admin_token="",
            api_token="",
            request_size_limit_bytes=1024 * 1024,
            cors_allow_origins=[],
            blueprint_repo="/tmp/blueprints",
            configured_blueprint_repo="/tmp/blueprints",
            dev_local_blueprint_repo="",
        )
        refreshed = ApiConfig(
            env="dev",
            host="localhost",
            port=54001,
            grpc_target="localhost:55051",
            grpc_timeout_seconds=10.0,
            grpc_auth_token="auth-from-state",
            grpc_admin_token="admin-from-state",
            api_token="",
            request_size_limit_bytes=1024 * 1024,
            cors_allow_origins=[],
            blueprint_repo="/tmp/blueprints",
            configured_blueprint_repo="/tmp/blueprints",
            dev_local_blueprint_repo="",
        )

        class CurrentClient:
            def __init__(
                self,
                target=None,
                timeout=None,
                auth_token=None,
                admin_token=None,
            ):
                calls.append(
                    {
                        "target": target,
                        "timeout": timeout,
                        "auth_token": auth_token,
                        "admin_token": admin_token,
                    }
                )

        state.Client = CurrentClient

        with patch.object(state.ApiConfig, "from_env", return_value=refreshed):
            state.get_client()

        self.assertEqual(
            calls,
            [
                {
                    "target": "localhost:55051",
                    "timeout": 10.0,
                    "auth_token": "auth-from-state",
                    "admin_token": "admin-from-state",
                }
            ],
        )
