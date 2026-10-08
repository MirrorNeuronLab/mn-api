from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mn_sdk.blueprints.authoring import write_blueprint_definition

from mn_api import state
from mn_api.app import create_app
from mn_api.routes.v1 import blueprints


@pytest.mark.parametrize("air_gapped", [True, False])
@pytest.mark.parametrize("skills", [[], [{"name": "mirrorneuron-video-analysis", "version": ">=1.3,<2", "source": "gar"}]])
def test_blueprint_list_and_detail_expose_review_metadata(monkeypatch, tmp_path, skills, air_gapped):
    write_blueprint_definition(tmp_path / "watch", {
        "apiVersion": "mn.workflow/v1", "kind": "Workflow", "id": "watch",
        "name": "Watch", "description": "Watch for changes.",
        "workflow": {"steps": [{"id": "finish"}]}, "agents": {}, "runtime": {}, "contract": {},
        "skill_dependencies": skills, "air-gapped": air_gapped,
    })
    (tmp_path / "index.json").write_text(json.dumps(["watch"]))
    config = SimpleNamespace(
        api_token="", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[],
        blueprint_source="local", blueprint_local=str(tmp_path),
    )
    monkeypatch.setattr(state, "config", config)
    monkeypatch.setattr(blueprints, "_config", lambda: config)
    monkeypatch.setenv("MN_BLUEPRINT_INSTALLS_DIR", str(tmp_path / "additions"))
    client = TestClient(create_app())

    listing = client.get("/api/v1/blueprints")
    detail = client.get("/api/v1/blueprints/watch")
    assert listing.status_code == detail.status_code == 200
    expected = [{"name": skill["name"], "version_constraint": skill["version"]} for skill in skills]
    for record in (listing.json()["items"][0], detail.json()):
        assert record["skills"] == expected
        assert record["air-gapped"] is air_gapped
        assert record["added"] is False


def test_review_metadata_is_described_in_openapi():
    schema = create_app().openapi()
    resource = schema["components"]["schemas"]["BlueprintResource"]["properties"]
    assert "skills" in resource
    assert "air-gapped" in resource
    skill = schema["components"]["schemas"]["BlueprintSkill"]
    assert skill["required"] == ["name", "version_constraint"]
    assert skill["additionalProperties"] is False
