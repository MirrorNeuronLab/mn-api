from mn_sdk.blueprint_source import normalize_blueprint
from mn_api.blueprint_additions import blueprint_public_projection


def test_public_catalog_preserves_collaboration_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("MN_BLUEPRINT_INSTALLS_DIR", str(tmp_path))
    contract = {
        "schema": "mn.collaboration.pair.v1", "topology": "pair",
        "protocol": "marketing.v1", "goalId": "marketing", "accepts": ["partner"],
        "goalKey": "inputs.payload.goal_id", "peerKey": "inputs.payload.peer_job_id",
        "commonGoalKey": "inputs.payload.common_goal", "sameRuntime": True,
    }
    normalized = normalize_blueprint({"id": "planner", "product": {
        "business_goal": "Market books", "collaboration": {**contract, "private": "omit"},
    }})
    public = blueprint_public_projection(normalized)
    assert public["collaboration"] == contract
    assert public["business_goal"] == "Market books"
    assert "private" not in public["collaboration"]
