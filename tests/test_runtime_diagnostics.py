from threading import Barrier

from mn_api.routes import system


def test_diagnostics_parallel_checks_keep_failures_and_order(monkeypatch):
    ready = Barrier(3, timeout=2)

    def runtime(**kwargs):
        ready.wait()
        return {"components": [{"name": "core_grpc", "status": "passing"}], "jobs": {"total": 2}}

    def docker():
        ready.wait()
        raise OSError("unavailable")

    def gateway(**kwargs):
        ready.wait()
        return {"status": "passing"}

    monkeypatch.setattr(system, "runtime_status", runtime)
    monkeypatch.setattr(system, "docker_status", docker)
    monkeypatch.setattr(system, "litellm_gateway_health", gateway)
    report = system.runtime_doctor(timeout=1)
    assert [item["name"] for item in report["components"]] == ["core_grpc", "docker_model_runner", "litellm_gateway"]
    assert report["overall"] == "critical"
    assert report["foundation"]["docker_model_runner"]["status"] == "critical"
    assert report["foundation"]["litellm_gateway"]["status"] == "passing"
    assert report["jobs"] == {"total": 2}
