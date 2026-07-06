from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from mn_api import state
from mn_api.routes import services


def test_service_check_rejects_missing_path_or_bundle(api_client):
    response = api_client.post("/api/v1/services/check", json={})

    assert response.status_code == 422
    assert response.json()["detail"] == "path or _bundle_path is required"


def test_service_check_rejects_missing_and_malformed_manifests(api_client, tmp_path):
    missing = tmp_path / "missing-manifest"
    missing.mkdir()
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "manifest.json").write_text("{bad", encoding="utf-8")
    non_object = tmp_path / "non-object"
    non_object.mkdir()
    (non_object / "manifest.json").write_text("[]", encoding="utf-8")

    assert api_client.post("/api/v1/services/check", json={"path": str(tmp_path / "nope")}).status_code == 400
    assert api_client.post("/api/v1/services/check", json={"path": str(missing)}).json()["detail"] == "bundle manifest.json not found"
    assert api_client.post("/api/v1/services/check", json={"path": str(malformed)}).json()["detail"] == "bundle manifest.json is malformed"
    assert api_client.post("/api/v1/services/check", json={"path": str(non_object)}).json()["detail"] == "bundle manifest.json must be an object"


def test_service_check_resolver_normalizes_runtime_registry(monkeypatch, api_client, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"graph_id": "g", "nodes": []}), encoding="utf-8")
    observed = {}

    class FakeClient:
        def resolve_service(self, name, tags=None, passing_only=True):
            observed["resolve"] = (name, tags, passing_only)
            return json.dumps({"services": [{"name": name, "tags": tags}]})

    def fake_validation(bundle_dir, manifest, **kwargs):
        observed["services"] = kwargs["resolver"]("vector-db", {"tags": ["rag"]})
        return {"ok": True}

    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr("mn_api.routes.services.run_service_validation", fake_validation)

    response = api_client.post("/api/v1/services/check", json={"path": str(bundle)})

    assert response.status_code == 200
    assert observed["resolve"] == ("vector-db", ["rag"], True)
    assert observed["services"] == [{"name": "vector-db", "tags": ["rag"]}]


def test_service_check_bundle_dir_validates_uploaded_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mn_api.routes.services.load_uploaded_bundle",
        lambda bundle_path, upload_root: ("{}", {}),
    )

    response = services._service_check_bundle_dir(type("Req", (), {"bundle_path": str(tmp_path), "path": None})())

    assert response == tmp_path.resolve()

    monkeypatch.setattr("mn_api.routes.services.load_uploaded_bundle", lambda bundle_path, upload_root: ("{bad", {}))
    with pytest.raises(HTTPException):
        services._service_check_bundle_dir(type("Req", (), {"bundle_path": str(tmp_path), "path": None})())

    monkeypatch.setattr("mn_api.routes.services.load_uploaded_bundle", lambda bundle_path, upload_root: ("[]", {}))
    with pytest.raises(HTTPException):
        services._service_check_bundle_dir(type("Req", (), {"bundle_path": str(tmp_path), "path": None})())
