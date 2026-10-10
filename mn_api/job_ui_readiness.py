"""Check a declared page through the same network path as its iframe."""

from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any

from mn_sdk.runtime_config import RuntimeConfig
from mn_sdk_web_ui import probe_web_ui

from mn_api.config import WebUiConfig


PROBE_HEADER = "X-MN-Job-UI-Probe"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def probe_job_web_ui(job_id: str, url: str) -> dict[str, Any]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return {"schema_version": "mn.web_ui.readiness.v1", "ready": False, "reason": "invalid_url"}
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or not 1 <= port <= 65535):
        return {"schema_version": "mn.web_ui.readiness.v1", "ready": False, "reason": "invalid_url"}

    base = RuntimeConfig.from_env().web_ui_url.rstrip("/")
    path = f"/job-ui-proxy/{urllib.parse.quote(job_id, safe='-._')}/{port}{parsed.path or '/'}"
    target = base + path + (f"?{parsed.query}" if parsed.query else "")
    token = WebUiConfig.from_env().api_token
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def open_probe(request, **kwargs):
        # Only the local proxy receives credentials. It strips them before
        # forwarding to the declared worker; redirects are never followed.
        request.add_header(PROBE_HEADER, "1")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return opener.open(request, **kwargs)

    return probe_web_ui(target, opener=open_probe)
