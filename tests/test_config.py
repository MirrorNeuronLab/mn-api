from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mn_api.config import ApiConfig


class TestApiConfig(unittest.TestCase):
    def test_token_files_win_over_stale_runtime_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".mn"
            state_dir.mkdir()
            (state_dir / "docker-compose.env").write_text(
                "MN_GRPC_AUTH_TOKEN=stale-auth-from-state\n"
                "MN_GRPC_ADMIN_TOKEN=stale-admin-from-state\n",
                encoding="utf-8",
            )
            (state_dir / "grpc_auth.token").write_text("auth-from-file\n", encoding="utf-8")
            (state_dir / "grpc_admin.token").write_text("admin-from-file\n", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": tmp}, clear=True):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-from-file")
        self.assertEqual(config.grpc_admin_token, "admin-from-file")

    def test_configured_token_file_env_wins_before_runtime_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".mn"
            state_dir.mkdir()
            (state_dir / "docker-compose.env").write_text(
                "MN_GRPC_AUTH_TOKEN=stale-auth-from-state\n"
                "MN_GRPC_ADMIN_TOKEN=stale-admin-from-state\n",
                encoding="utf-8",
            )
            auth_file = Path(tmp) / "auth.token"
            admin_file = Path(tmp) / "admin.token"
            auth_file.write_text("auth-from-configured-file\n", encoding="utf-8")
            admin_file.write_text("admin-from-configured-file\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "MN_GRPC_AUTH_TOKEN_FILE": str(auth_file),
                    "MN_GRPC_ADMIN_TOKEN_FILE": str(admin_file),
                },
                clear=True,
            ):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_auth_token, "auth-from-configured-file")
        self.assertEqual(config.grpc_admin_token, "admin-from-configured-file")

    def test_runtime_endpoints_override_stale_runtime_grpc_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".mn"
            state_dir.mkdir()
            (state_dir / "docker-compose.env").write_text(
                "MN_GRPC_TARGET=localhost:55051\n",
                encoding="utf-8",
            )
            (state_dir / "runtime-endpoints.json").write_text(
                '{"grpc":{"target":"192.168.4.20:55051"}}\n',
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"HOME": tmp}, clear=True):
                config = ApiConfig.from_env()

        self.assertEqual(config.grpc_target, "192.168.4.20:55051")


if __name__ == "__main__":
    unittest.main()
