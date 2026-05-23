from __future__ import annotations

import unittest
from types import SimpleNamespace

from mn_api import state


class TestStateClient(unittest.TestCase):
    def setUp(self):
        self.original_config = state.config
        self.original_client = state._client
        self.original_client_class = state.Client
        state._client = None
        state.config = SimpleNamespace(
            grpc_target="localhost:50051",
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
                    "target": "localhost:50051",
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
                    "target": "localhost:50051",
                    "timeout": 10.0,
                    "auth_token": "",
                    "admin_token": "admin-secret",
                }
            ],
        )
