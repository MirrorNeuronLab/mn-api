from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from mn_api.blueprint_secret_environment import (
    inject_declared_secret_environment,
    manifest_without_secret_environment,
    requested_secret_environment,
    validate_blueprint_secret_environment,
)


def test_requested_secret_environment_unwraps_bounded_secret_values():
    requested = {
        "MN_SMTP_USERNAME": SecretStr("sender@example.invalid"),
        "MN_SMTP_PASSWORD": SecretStr("test-app-password"),
    }

    assert requested_secret_environment(requested) == {
        "MN_SMTP_USERNAME": "sender@example.invalid",
        "MN_SMTP_PASSWORD": "test-app-password",
    }
    assert requested_secret_environment(None) == {}
    assert "test-app-password" not in repr(requested)


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        ({"bad-name": SecretStr("value")}, "name is invalid"),
        ({"VALID_NAME": SecretStr("")}, "is empty"),
        ({f"SECRET_{index}": SecretStr("value") for index in range(17)}, "Too many"),
        ({"VALID_NAME": SecretStr("x" * 8193)}, "is too large"),
    ],
)
def test_requested_secret_environment_rejects_invalid_bounds(requested, message):
    with pytest.raises(HTTPException, match=message):
        requested_secret_environment(requested)


def test_blueprint_secret_environment_requires_a_declared_pass_env_name():
    manifest = {
        "workers": {
            "groups": [
                {"with": {"pass_env": ["DECLARED_SECRET", "not-valid"]}},
            ]
        }
    }

    validate_blueprint_secret_environment(manifest, {"DECLARED_SECRET": "value"})
    validate_blueprint_secret_environment(manifest, {})
    with pytest.raises(HTTPException, match="not declared"):
        validate_blueprint_secret_environment(manifest, {"OTHER_SECRET": "value"})


def test_secret_environment_is_injected_only_into_matching_runtime_nodes_and_redacted():
    manifest = {
        "agents": {
            "nodes": [
                {
                    "node_id": "delivery",
                    "config": {
                        "pass_env": ["DECLARED_SECRET"],
                        "environment": {"SAFE": "value"},
                    },
                },
                {"node_id": "analysis", "config": {"pass_env": [], "environment": {}}},
            ],
            "extra_nodes": [],
        }
    }

    original_json = json.dumps(manifest)
    assert inject_declared_secret_environment(original_json, {}) == original_json
    injected_json = inject_declared_secret_environment(original_json, {"DECLARED_SECRET": "secret-value"})
    injected = json.loads(injected_json)
    public = manifest_without_secret_environment(injected_json, {"DECLARED_SECRET": "secret-value"})

    assert injected["agents"]["nodes"][0]["config"]["environment"] == {
        "SAFE": "value",
        "DECLARED_SECRET": "secret-value",
    }
    assert injected["agents"]["nodes"][1]["config"]["environment"] == {}
    assert public["agents"]["nodes"][0]["config"]["environment"] == {"SAFE": "value"}
    assert "secret-value" not in json.dumps(public)


def test_secret_environment_rejects_missing_worker_or_invalid_environment_shape():
    with pytest.raises(HTTPException, match="no executable worker"):
        inject_declared_secret_environment(
            json.dumps(
                {
                    "agents": {
                        "nodes": [
                            {"node_id": "worker", "config": {"pass_env": []}}
                        ]
                    }
                }
            ),
            {"DECLARED_SECRET": "value"},
        )

    with pytest.raises(HTTPException, match="environment is invalid"):
        inject_declared_secret_environment(
            json.dumps(
                {
                    "agents": {
                        "nodes": [
                            {
                                "node_id": "worker",
                                "config": {
                                    "pass_env": ["DECLARED_SECRET"],
                                    "environment": "invalid",
                                },
                            }
                        ]
                    }
                }
            ),
            {"DECLARED_SECRET": "value"},
        )
