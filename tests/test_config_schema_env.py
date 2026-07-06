from __future__ import annotations

from pathlib import Path

import pytest

from mn_api.config_env import load_config_source, parse_dotenv_line, profile_name, read_dotenv
from mn_api.config_schema import (
    ConfigError,
    parse_bool,
    parse_float,
    parse_int,
    parse_list,
    parse_path,
    parse_url,
    redact_config_values,
)


def test_config_schema_parsers_accept_valid_values():
    assert parse_int("PORT", " 42 ") == 42
    assert parse_float("TIMEOUT", " 2.5 ") == 2.5
    assert parse_bool("FLAG", "yes") is True
    assert parse_bool("FLAG", "off") is False
    assert parse_list("ITEMS", " a, b ,, c ") == ["a", "b", "c"]
    assert parse_url("URL", "https://example.com/") == "https://example.com"
    assert parse_path("PATH", "~/data") == Path("~/data").expanduser()


@pytest.mark.parametrize(
    ("parser", "value"),
    [
        (parse_int, "x"),
        (parse_float, "x"),
        (parse_bool, "maybe"),
        (parse_url, "ftp://example.com"),
        (parse_url, "example.com"),
        (parse_path, ""),
    ],
)
def test_config_schema_parsers_raise_clear_errors(parser, value):
    with pytest.raises(ConfigError):
        parser("SETTING", value)


def test_sensitive_config_values_are_redacted_by_name_and_marker():
    assert redact_config_values(
        {
            "MN_API_TOKEN": "secret",
            "CUSTOM_PASSWORD": "pw",
            "NORMAL": "value",
            "EMPTY_SECRET": "",
        }
    ) == {
        "MN_API_TOKEN": "<redacted>",
        "CUSTOM_PASSWORD": "<redacted>",
        "NORMAL": "value",
        "EMPTY_SECRET": "",
    }


def test_dotenv_line_parser_handles_exports_quotes_and_comments():
    assert parse_dotenv_line("") is None
    assert parse_dotenv_line("# comment") is None
    assert parse_dotenv_line("not-an-assignment") is None
    assert parse_dotenv_line("=missing") is None
    assert parse_dotenv_line("export A=1 # comment") == ("A", "1")
    assert parse_dotenv_line("B='two # still value'") == ("B", "two # still value")
    assert parse_dotenv_line('C="three"') == ("C", "three")


def test_read_dotenv_and_load_config_source_layer_values(tmp_path):
    (tmp_path / ".env").write_text("A=base\nB=base\n", encoding="utf-8")
    (tmp_path / ".env.prod").write_text("B=profile\nC=profile\n", encoding="utf-8")

    source = load_config_source(env={"MN_ENV": "production", "C": "real"}, env_dir=tmp_path)

    assert profile_name("production") == "prod"
    assert read_dotenv(tmp_path / ".env") == {"A": "base", "B": "base"}
    assert source.mn_env == "prod"
    assert source.effective_env["A"] == "base"
    assert source.effective_env["B"] == "profile"
    assert source.effective_env["C"] == "real"
    assert source.loaded_files == (tmp_path / ".env", tmp_path / ".env.prod")
