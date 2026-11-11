"""Native SDK contract fake for surface authentication and MCP protocol tests."""

from dataclasses import replace
import json
from mn_api import state
from mn_sdk.runtime_control import RuntimeControlClient
from mn_sdk_common.mcp import SUPERVISION_PROFILE


class MCPRuntime:
    def __init__(self):
        self.jobs = {
            "job-1": {
                "job_id": "job-1",
                "blueprint_id": "chat-blueprint",
                "mcp": {
                    "profile": SUPERVISION_PROFILE,
                    "manual": "@/knowledge/user_manual.md",
                    "tools": {},
                    "manual_content": "# Test co-worker\nReview published evidence.",
                    "manual_sha256": "test",
                },
            }
        }
        self.calls = []

    def get_job(self, job_id, **_):
        return json.dumps(self.jobs.get(job_id, {"job_id": job_id, "mcp": self.jobs["job-1"]["mcp"]}))


def _configure(monkeypatch, runtime, token=""):
    monkeypatch.setattr(state, "config", replace(state.config, api_token=token))
    monkeypatch.setattr(state, "client", runtime)
    monkeypatch.setattr(RuntimeControlClient, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(RuntimeControlClient, "close", lambda self: None)

    def command(self, action, arguments=None, **_):
        values = arguments or {}
        runtime.calls.append((action, values))
        method = values["method"]
        job_id = values["job_id"]
        if action == "mcp.context":
            if method == "response_mode":
                return "base"
            if method == "get_pending_human_request":
                return None
            raise AssertionError(method)
        if method == "identity":
            return {"job_id": job_id, "blueprint_id": "chat-blueprint"}
        if method == "declaration":
            return runtime.jobs["job-1"]["mcp"]
        if method == "capabilities":
            return {"available": True, "domain_available": False, "latest_run_id": None, "readiness": {}}
        if method == "invoke" and values["name"] == "get_user_manual":
            return {"manual": runtime.jobs["job-1"]["mcp"]["manual_content"], "sha256": "test"}
        raise AssertionError((action, values))

    monkeypatch.setattr(RuntimeControlClient, "command", command)


def _mcp_request(client, job_id, body, token=""):
    if not token and not state.config.api_token:
        grant = client.post(f"/api/v1/jobs/{job_id}/mcp-sessions")
        if grant.status_code != 200:
            return grant
        token = grant.json()["access_token"]
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    meta = body.get("params", {}).get("_meta", {})
    if meta.get("io.modelcontextprotocol/protocolVersion"):
        from mcp.shared.inbound import NAME_BEARING_METHODS

        headers["MCP-Protocol-Version"] = meta["io.modelcontextprotocol/protocolVersion"]
        headers["MCP-Method"] = body["method"]
        if key := NAME_BEARING_METHODS.get(body["method"]):
            headers["MCP-Name"] = body["params"][key]
    return client.post(f"/api/v1/jobs/{job_id}/mcp", headers=headers, json=body)
