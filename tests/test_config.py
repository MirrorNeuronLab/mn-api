from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mn_api.config import ApiConfig, ConfigError, WebUiConfig, safe_config_values, subprocess_environment
from mn_api.config_env import load_config_source


class TestApiConfig(unittest.TestCase):
    def test_dotenv_defaults_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("MN_API_HOST=127.0.0.1\nMN_API_PORT=8000\n", encoding="utf-8")

            config = ApiConfig.from_env(env={"HOME": tmp}, env_dir=root)

        self.assertEqual(config.env, "dev")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)

    def test_profile_dotenv_overrides_base_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("MN_API_PORT=8000\nMN_LOG_LEVEL=info\n", encoding="utf-8")
            (root / ".env.test").write_text("MN_API_PORT=9000\n", encoding="utf-8")

            config = ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "test"}, env_dir=root)

        self.assertEqual(config.env, "test")
        self.assertEqual(config.port, 9000)

    def test_real_environment_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("MN_API_PORT=8000\n", encoding="utf-8")

            config = ApiConfig.from_env(env={"HOME": tmp, "MN_API_PORT": "8080"}, env_dir=root)

        self.assertEqual(config.port, 8080)

    def test_mn_env_defaults_to_dev_when_unset(self):
        source = load_config_source(env={}, env_dir=tempfile.gettempdir())

        self.assertEqual(source.mn_env, "dev")

    def test_dev_alias_loads_dotenv_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.dev").write_text("MN_API_PORT=8100\n", encoding="utf-8")

            dev_config = ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "dev"}, env_dir=root)
            development_config = ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "development"}, env_dir=root)

        self.assertEqual(dev_config.port, 8100)
        self.assertEqual(development_config.port, 8100)

    def test_test_env_loads_dotenv_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.test").write_text("MN_API_PORT=8200\n", encoding="utf-8")

            config = ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "test"}, env_dir=root)

        self.assertEqual(config.port, 8200)

    def test_prod_aliases_load_dotenv_prod_if_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.prod").write_text("MN_API_TOKEN=prod-token\nMN_API_PORT=8300\n", encoding="utf-8")

            prod_config = ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "prod"}, env_dir=root)
            production_config = ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "production"}, env_dir=root)

        self.assertEqual(prod_config.env, "prod")
        self.assertEqual(prod_config.port, 8300)
        self.assertEqual(production_config.env, "prod")
        self.assertEqual(production_config.port, 8300)

    def test_production_does_not_require_dotenv_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ApiConfig.from_env(
                env={"HOME": tmp, "MN_ENV": "production", "MN_API_TOKEN": "prod-token"},
                env_dir=tmp,
            )

        self.assertEqual(config.env, "prod")
        self.assertEqual(config.api_token, "prod-token")

    def test_missing_required_prod_token_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "MN_API_TOKEN is required"):
                ApiConfig.from_env(env={"HOME": tmp, "MN_ENV": "production"}, env_dir=tmp)

    def test_invalid_type_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "MN_API_PORT must be an integer"):
                ApiConfig.from_env(env={"HOME": tmp, "MN_API_PORT": "not-an-int"}, env_dir=tmp)

    def test_secret_values_are_redacted_from_safe_snapshot(self):
        safe_values = safe_config_values(env={"MN_API_TOKEN": "super-secret", "MN_API_PORT": "8080"})

        self.assertEqual(safe_values["MN_API_TOKEN"], "<redacted>")
        self.assertNotIn("super-secret", repr(safe_values))
        self.assertEqual(safe_values["MN_API_PORT"], "8080")

    def test_config_module_is_reusable_for_web_ui_and_child_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = Path(tmp) / ".mn"
            state_dir.mkdir()
            (state_dir / "api.token").write_text("web-api-token-from-file\n", encoding="utf-8")
            (root / ".env").write_text(
                "MN_API_HOST=127.0.0.1\nMN_API_PORT=8010\nMN_WEB_UI_PORT=55180\n",
                encoding="utf-8",
            )
            env = {"HOME": tmp}

            api_config = ApiConfig.from_env(env=env, env_dir=root)
            web_config = WebUiConfig.from_env(env=env, env_dir=root)
            with patch("mn_api.config.load_config_source", return_value=load_config_source(env=env, env_dir=root)):
                child_env = subprocess_environment()

        self.assertEqual(api_config.port, 8010)
        self.assertEqual(web_config.api_base_url, "http://127.0.0.1:8010/api/v1")
        self.assertEqual(web_config.api_token, "web-api-token-from-file")
        self.assertEqual(web_config.port, 55180)
        self.assertEqual(child_env["MN_API_PORT"], "8010")

    def test_local_token_files_override_stale_runtime_env(self):
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

    def test_configured_token_files_override_stale_runtime_env(self):
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
