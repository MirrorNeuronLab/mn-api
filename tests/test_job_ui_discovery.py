from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mn_api import state
from mn_api.routes import jobs


@pytest.mark.parametrize("owner_is_local", [True, False], ids=["local-owner", "federated-owner"])
@pytest.mark.parametrize("check_readiness", [True, False], ids=["discovery", "proxy-descriptor"])
def test_job_ui_ignores_unavailable_unrelated_peers(
    api_client, monkeypatch, tmp_path, owner_is_local, check_readiness
):
    owner = "mirror_neuron@10.0.4.27" if owner_is_local else "mirror_neuron@10.0.4.32"
    host = owner.split("@", 1)[1]
    queries = []

    class FakeClient:
        def get_job(self, job_id):
            assert job_id == "job-1"
            return json.dumps({"latest_run_id": "execution-1", "owner_node": owner})

        def list_services(self, **query):
            queries.append(query)
            if query.get("node") != owner:
                raise RuntimeError("an unrelated federation peer timed out")
            return json.dumps({"items": [{
                "job_id": "execution-1", "node": owner, "name": "controller-ui",
                "port": 8088, "status": "passing", "tags": ["web_ui"],
            }]})

        def get_system_summary(self):
            return json.dumps({"nodes": [
                {"name": owner, "address": host, "self?": owner_is_local},
                {"name": "mirror_neuron@unavailable", "address": "unavailable", "self?": False},
            ]})

    probes = []
    monkeypatch.setattr(
        state, "config",
        SimpleNamespace(api_token="test-token", request_size_limit_bytes=100_000),
    )
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(jobs, "job_data_dir_from_id", lambda *_args, **_kwargs: tmp_path / "missing")
    monkeypatch.setattr(jobs, "shared_job_ui_dir_from_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        jobs, "probe_job_web_ui",
        lambda job_id, url: probes.append((job_id, url)) or {"ready": True, "reason": "ready"},
    )
    # Keep this HTTP boundary test independent of remote handle retrieval.
    monkeypatch.setattr(jobs, "_owner_node_job_ui_handle", lambda _job_id: None)

    response = api_client.get(
        f"/api/v1/jobs/job-1/ui?check_readiness={str(check_readiness).lower()}",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json()["web_ui"]["url"] == f"http://{host}:8088"
    assert response.json()["web_ui"]["status"] == "running"
    assert queries == [{
        "job_id": "execution-1", "node": owner, "passing_only": True, "page_size": 200,
    }]
    assert probes == ([("job-1", f"http://{host}:8088")] if check_readiness else [])


@pytest.mark.parametrize("owner", [None, "", "  "])
def test_job_ui_without_an_owner_does_not_broadcast_service_discovery(monkeypatch, tmp_path, owner):
    class FakeClient:
        def get_job(self, _job_id):
            return json.dumps({"latest_run_id": "execution-1", "owner_node": owner})

        def list_services(self, **_query):
            pytest.fail("UI discovery must not query every federation peer without an owner")

    handle = {"job_id": "job-1", "url": "http://worker:8088/", "status": "running"}
    (tmp_path / "ui.json").write_text(json.dumps({"job_id": "job-1"}), encoding="utf-8")
    (tmp_path / "web_ui.json").write_text(json.dumps(handle), encoding="utf-8")
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(jobs, "job_data_dir_from_id", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(jobs, "shared_job_ui_dir_from_id", lambda *_args, **_kwargs: None)

    assert jobs.get_job_ui("job-1", check_readiness=False)["web_ui"] == handle
