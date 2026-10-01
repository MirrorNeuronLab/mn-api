from types import SimpleNamespace

from fastapi.testclient import TestClient
from mn_api import state
from mn_api.app import create_app
from mn_api.job_mcp import JobContextProvider
from mn_api.routes.v1 import assistance


def configure(monkeypatch, *, status="completed", worker_type="batch", freshness="fresh"):
    monkeypatch.setattr(state, "config", SimpleNamespace(api_token="secret-test", request_size_limit_bytes=1024 * 1024, cors_allow_origins=[]))
    monkeypatch.setattr(state, "refresh_config_from_env", lambda: state.config)
    monkeypatch.setattr(assistance, "find_blueprint", lambda config, identity: ("root", {"id": identity, "type": worker_type}))
    monkeypatch.setattr(assistance.provider, "get_pending_human_request", lambda identity: None)
    monkeypatch.setattr(assistance.provider, "get_context", lambda *args, **kwargs: {
        "identity": {"job_id": "job-1", "blueprint_id": "blueprint-1"}, "freshness": {"state": freshness},
        "profile": {"type": worker_type}, "latest_run": {"run_id": "run-1", "status": status}})
    return TestClient(create_app())


def post(client, **extra):
    return client.post("/api/v1/assistance/evaluations", headers={"Authorization": "Bearer secret-test"},
                       json={"blueprint_id": "blueprint-1", "job_id": "job-1", "execution_id": "run-1", "setup": {"ready": True, "mode": "real"}, **extra})


def test_authentication_and_untrusted_operational_input(monkeypatch):
    client = configure(monkeypatch)
    assert client.post("/api/v1/assistance/evaluations", json={"blueprint_id": "blueprint-1"}).status_code == 401
    assert post(client, context={"latest_run": {"status": "completed"}}).status_code == 422
    assert post(client, setup={"password": "hidden"}).status_code == 422


def test_runtime_facts_control_opportunities(monkeypatch):
    client = configure(monkeypatch, status="failed")
    result = post(client).json()
    assert result["opportunity"]["kind"] == "diagnose"
    assert result["context"]["run_status"] == "failed"
    assert post(client, execution_id="other-run").status_code == 409
    assert post(client, blueprint_id="other-blueprint").status_code == 409


def test_schedule_service_and_stale_context(monkeypatch):
    assert post(configure(monkeypatch)).json()["opportunity"]["kind"] == "schedule"
    assert post(configure(monkeypatch, worker_type="service")).json()["opportunity"] is None
    assert post(configure(monkeypatch, freshness="unavailable")).json()["opportunity"] is None
    assert post(configure(monkeypatch), setup={"ready": True}).json()["opportunity"] is None


def test_local_setup_can_be_helped_before_job_creation(monkeypatch):
    client = configure(monkeypatch)
    result = post(client, job_id=None, execution_id=None, setup={"ready": False}).json()
    assert result["opportunity"]["kind"] == "setup"
    assert result["context"]["freshness"] == "unavailable"


def test_context_only_provider_does_not_enable_job_responses(monkeypatch):
    class Runtime:
        def get_job(self, identity): return {"job_id": identity, "blueprint_id": "plain", "status": "active"}
    from mn_api import job_mcp
    monkeypatch.setattr(JobContextProvider, "_service", lambda self: Runtime())
    monkeypatch.setattr(job_mcp, "find_blueprint", lambda config, identity: ("root", {"id": identity, "response_service": {"enabled": False}}))
    provider = JobContextProvider(require_response=False)
    job, blueprint, descriptor = provider._job_and_blueprint("job-plain")
    assert job["job_id"] == "job-plain"
    assert descriptor["response_enabled"] is False
