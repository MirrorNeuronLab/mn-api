"""Single-owner reads for canonical run-file resources, without replica waits."""

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from mn_api import state
from mn_api.contracts import API_CONTRACT
from mn_api.node_api import node_api_base_url

HOP_HEADER = "X-MN-Run-File-Hop"
BINDING_HEADER = "X-MN-Run-File-Binding"
_RESPONSE_HEADERS = {"content-type", "content-length", "content-disposition", "content-range",
                     "accept-ranges", "etag", "last-modified", "cache-control"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class _OwnerFileResponse(StreamingResponse):
    def __init__(self, upstream, content, **options):
        self._upstream = upstream
        super().__init__(content, **options)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._upstream.close()


def _binding(run, owner):
    value = {"run_id": run.get("run_id"), "job_id": run.get("job_id"), "owner_node": owner,
             "data_generation": run.get("data_generation"),
             "reference": run.get("run_data_ref") or run.get("result_ref")}
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError) as error:
        raise HTTPException(503, "The run's file binding is invalid.") from error
    return hashlib.sha256(encoded).hexdigest()


def owner_file_response(run_id, run, request, resource):
    """Return a peer response, or let local/legacy owners use verified storage.

    Core selects the owner and endpoint. Only the existing authenticated run
    resources call this helper; callers cannot supply a target URL or host path.
    """
    hop = request.headers.get(HOP_HEADER)
    admission = run.get("admission")
    owner = run.get("owner_node") or (admission.get("owner_node") if isinstance(admission, dict) else None)
    if not owner:
        if hop is not None:
            raise HTTPException(409, "The run's file owner changed. Try again.")
        return None
    if run.get("run_id") != run_id:
        raise HTTPException(502, "The runtime returned a different run.")
    if hop not in (None, "1"):
        raise HTTPException(400, "Invalid run-file routing hop.")
    binding = _binding(run, owner)
    try:
        summary = state.client.get_system_summary()
        summary = json.loads(summary) if isinstance(summary, (str, bytes)) else summary
        nodes = summary.get("nodes", [])
        node = next((item for item in nodes if isinstance(item, dict) and item.get("name") == owner), None)
    except Exception as error:
        raise HTTPException(503, "The computer holding the run files is unavailable.") from error
    if node is None:
        raise HTTPException(503, "The computer holding the run files is unavailable.")
    local = node.get("self?") is True or node.get("self") is True
    if hop is not None:
        if not local or request.headers.get(BINDING_HEADER) != binding:
            raise HTTPException(409, "The run's file owner or storage binding changed. Try again.")
        request.state.run_file_binding = binding
        return None
    if local:
        return None
    hardware = node.get("hardware")
    endpoint = hardware.get("http_api") if isinstance(hardware, dict) else None
    # Older owners and deliberately disabled HTTP services retain the existing
    # exact-binding replica reader. An advertised enabled endpoint must work;
    # its failure never substitutes a possibly stale local copy.
    if endpoint is None or (isinstance(endpoint, dict) and endpoint.get("enabled") is False):
        return None
    base = node_api_base_url(node)
    url = f"{base}/runs/{urllib.parse.quote(run_id, safe='')}/{resource}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {HOP_HEADER: "1", BINDING_HEADER: binding, "Accept-Encoding": "identity"}
    token = str(getattr(state.refresh_config_from_env(), "api_token", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for name in ("Range", "If-Range"):
        if request.headers.get(name):
            headers[name] = request.headers[name]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        upstream = opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=15.0)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 404:
            raise HTTPException(404, "The run file is not available on its owner yet.") from error
        if code == 409:
            raise HTTPException(409, "The run's file owner or storage binding changed. Try again.") from error
        if code == 416:
            raise HTTPException(416, "The requested run-file range is unavailable.") from error
        if code in (400, 422):
            raise HTTPException(code, "Invalid run-file path or request.") from error
        raise HTTPException(503, "Couldn't read run files from their owner. Try again.") from error
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise HTTPException(503, "Couldn't read run files from their owner. Try again.") from error
    if (upstream.status not in (200, 206)
            or upstream.headers.get("X-API-Contract") != API_CONTRACT
            or upstream.headers.get(BINDING_HEADER) != binding
            or upstream.headers.get("Content-Encoding", "identity").lower() not in ("", "identity")):
        upstream.close()
        raise HTTPException(502, "The run-file owner did not confirm this storage binding. Update its API.")

    def chunks():
        try:
            # read1 returns after one buffered socket read, so a disconnected
            # caller can close the upstream between chunks without waiting for
            # an entire 64-KiB buffer to fill.
            while data := upstream.read1(64 * 1024):
                yield data
        finally:
            upstream.close()

    return _OwnerFileResponse(upstream, chunks(), status_code=upstream.status,
        headers={key: value for key, value in upstream.headers.items() if key.lower() in _RESPONSE_HEADERS},
        background=BackgroundTask(upstream.close))
