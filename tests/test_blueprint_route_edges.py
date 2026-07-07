from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from mn_api.routes import blueprints


def test_blueprint_cleanup_dry_run_and_unsupported_scope(api_client):
    dry_run = api_client.post("/api/v1/blueprints:cleanup", json={"dry_run": True})
    unsupported = api_client.post("/api/v1/blueprints:cleanup", json={"blueprint_id": "bp"})

    assert dry_run.status_code == 200
    assert dry_run.json()["status"] == "planned"
    assert unsupported.status_code == 501
    assert unsupported.json()["detail"]["error"] == "unsupported_api_cleanup_scope"


def test_blueprint_cleanup_stale_process_scope(monkeypatch, api_client, tmp_path):
    calls = []
    monkeypatch.setattr("mn_api.routes.blueprints.find_blueprint", lambda config, blueprint_id: (tmp_path, {"id": blueprint_id}))
    monkeypatch.setattr("mn_api.routes.blueprints.runtime_active_job_ids", lambda: {"active-job"})
    monkeypatch.setattr(
        "mn_api.routes.blueprints.cleanup_stale_blueprint_run_processes",
        lambda repo_root, blueprint, active_job_ids=None, reason="": calls.append((repo_root, blueprint, active_job_ids, reason)),
    )

    response = api_client.post(
        "/api/v1/blueprints:cleanup",
        json={"blueprint_id": "bp", "include_files": False, "include_docker": False},
    )

    assert response.status_code == 200
    assert response.json()["cleaned"] == "stale_processes"
    assert calls == [(tmp_path, {"id": "bp"}, {"active-job"}, "api_blueprint_cleanup")]


def test_blueprint_update_and_uninstall_report_unsupported_scope(api_client):
    update = api_client.post("/api/v1/blueprints:update", json={"source": "github"})
    uninstall = api_client.post("/api/v1/blueprints:uninstall", json={"blueprint_id": "bp", "dry_run": True})

    assert update.status_code == 501
    assert update.json()["detail"]["source"] == "github"
    assert uninstall.status_code == 501
    assert uninstall.json()["detail"]["blueprint_id"] == "bp"
    assert uninstall.json()["detail"]["dry_run"] is True


def test_blueprint_list_and_health_refresh_local_source_env(monkeypatch, api_client, tmp_path):
    catalog_a = tmp_path / "catalog-a"
    catalog_b = tmp_path / "catalog-b"
    catalog_a.mkdir()
    catalog_b.mkdir()
    (catalog_a / "index.json").write_text(json.dumps([{"id": "bp-a", "name": "Blueprint A"}]), encoding="utf-8")
    (catalog_b / "index.json").write_text(json.dumps([{"id": "bp-b", "name": "Blueprint B"}]), encoding="utf-8")

    monkeypatch.setenv("MN_ENV", "dev")
    monkeypatch.setenv("MN_BLUEPRINT_SOURCE", "local")
    monkeypatch.setenv("MN_BLUEPRINT_LOCAL", str(catalog_a))
    first = api_client.get("/api/v1/blueprints")

    monkeypatch.setenv("MN_BLUEPRINT_LOCAL", str(catalog_b))
    second = api_client.get("/api/v1/blueprints")
    health = api_client.get("/api/v1/health")

    assert first.status_code == 200
    assert first.json()["repo_dir"] == str(catalog_a.resolve())
    assert [blueprint["id"] for blueprint in first.json()["blueprints"]] == ["bp-a"]
    assert second.status_code == 200
    assert second.json()["repo_dir"] == str(catalog_b.resolve())
    assert [blueprint["id"] for blueprint in second.json()["blueprints"]] == ["bp-b"]
    assert health.status_code == 200
    assert health.json()["blueprint_source"] == "local"
    assert health.json()["active_blueprint_location"] == str(catalog_b.resolve())


def test_launch_progress_helpers_record_read_and_summarize(monkeypatch, tmp_path):
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path)

    assert blueprints.validate_progress_id(None) is None
    assert blueprints.validate_progress_id(" launch-1 ") == "launch-1"
    with pytest.raises(HTTPException):
        blueprints.validate_progress_id("bad id")

    blueprints.record_launch_progress("launch-1", "resolve_source", "running", "Resolving", label="Resolve")
    blueprints.record_launch_progress("launch-1", "launch", "completed", "Done", {"job_id": "job-1"})
    (tmp_path / "launch-1.jsonl").write_text((tmp_path / "launch-1.jsonl").read_text() + "bad-json\n", encoding="utf-8")

    events = blueprints.read_launch_progress("launch-1")
    phases = blueprints.summarize_launch_progress_phases(events)

    assert len(events) == 2
    assert phases[-1]["id"] == "launch"
    assert phases[-1]["status"] == "completed"
    assert blueprints.launch_progress_phase_label("custom_phase") == "Custom Phase"


def test_launch_progress_route_reports_completed(monkeypatch, api_client, tmp_path):
    monkeypatch.setattr("mn_api.routes.blueprints.launch_progress_root", lambda: tmp_path)
    blueprints.record_launch_progress("launch-route", "launch", "failed", "Nope")

    response = api_client.get("/api/v1/blueprints/launch/progress/launch-route")

    assert response.status_code == 200
    assert response.json()["completed"] is True
    assert response.json()["status"] == "failed"


def test_resolve_launch_source_validates_required_fields(monkeypatch):
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "catalog", "blueprint_id": None})())
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "path", "path": None})())
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "bundle", "bundle_path": None})())
    with pytest.raises(HTTPException):
        blueprints.resolve_launch_source(type("Req", (), {"source": "other"})())

    monkeypatch.setattr("mn_api.routes.blueprints.load_uploaded_bundle", lambda bundle_path, upload_root: (json.dumps({"graph_id": "g"}), {}))
    resolved = blueprints.resolve_launch_source(type("Req", (), {"source": "bundle", "bundle_path": "/tmp/uploaded"})())

    assert resolved["source"] == "bundle"
    assert resolved["blueprint"]["id"] == "g"
