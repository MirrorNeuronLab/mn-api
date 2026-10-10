from __future__ import annotations

from email.message import Message
from types import SimpleNamespace

import pytest

from mn_api import job_ui_readiness as readiness


class Reply:
    status = 200

    def __init__(self, content_type, body):
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.body = body

    def read1(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.mark.parametrize("asset_available", [True, False])
def test_readiness_probes_private_worker_page_and_assets_through_iframe(monkeypatch, asset_available):
    calls = []
    base = "http://localhost:55173/job-ui-proxy/job-1/8080"

    def open_url(request, *, timeout):
        calls.append(request)
        assert 0 < timeout <= 3
        if request.full_url == base + "/dashboard/?control=1":
            return Reply("text/html", b'<html><script src="./app.js"></script></html>')
        assert request.full_url == base + "/dashboard/app.js"
        return Reply("text/javascript" if asset_available else "text/html", b"code")

    monkeypatch.setattr(readiness.RuntimeConfig, "from_env", lambda: SimpleNamespace(web_ui_url="http://localhost:55173"))
    monkeypatch.setattr(readiness.WebUiConfig, "from_env", lambda: SimpleNamespace(api_token="test-token"))
    monkeypatch.setattr(readiness.urllib.request, "build_opener", lambda *_args: SimpleNamespace(open=open_url))
    result = readiness.probe_job_web_ui("job-1", "http://docker-only-worker:8080/dashboard/?control=1")
    assert result["ready"] is asset_available
    assert result["reason"] == ("ready" if asset_available else "assets_unavailable")
    assert len(calls) == 2
    assert all(request.get_header("Authorization") == "Bearer test-token" for request in calls)
    assert all(request.get_header("X-mn-job-ui-probe") == "1" for request in calls)


@pytest.mark.parametrize("url", ["http://[invalid", "http://worker:99999/", "http://user:secret@worker/", "file:///tmp/page"])
def test_readiness_rejects_invalid_worker_urls(monkeypatch, url):
    monkeypatch.setattr(readiness.RuntimeConfig, "from_env", lambda: pytest.fail("invalid URL must not be requested"))
    assert readiness.probe_job_web_ui("job-1", url)["reason"] == "invalid_url"
