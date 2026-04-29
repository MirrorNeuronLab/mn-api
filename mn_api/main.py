from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
import json
import tempfile
import uvicorn
import zipfile
from pathlib import Path
from mn_sdk import Client
import grpc
from mn_api.config import ApiConfig, auth_enabled
from mn_api.logging_config import configure_logging

config = ApiConfig.from_env()
logger = configure_logging()
app = FastAPI(title="MirrorNeuron API", version="1.0")
client = Client(target=config.grpc_target, timeout=config.grpc_timeout_seconds)
BUNDLE_UPLOAD_ROOT = Path(tempfile.gettempdir()) / "mirror_neuron_api_bundles"

if config.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    try:
        request_size = int(content_length) if content_length else 0
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_content_length"},
        )

    if request_size > config.request_size_limit_bytes:
        return JSONResponse(
            status_code=413,
            content={
                "error": "request_too_large",
                "limit_bytes": config.request_size_limit_bytes,
            },
        )
    return await call_next(request)


def require_auth(authorization: str = Header(default="")):
    if not auth_enabled(config):
        return None

    expected = f"Bearer {config.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return None

class SubmitJobRequest(BaseModel):
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = {}
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")

def handle_grpc_error(e: Exception):
    logger.exception("Request failed")
    if isinstance(e, grpc.RpcError) and e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return JSONResponse(
            status_code=503,
            content={"error": "resource_overloaded", "detail": e.details()},
        )

    if hasattr(e, 'details'):
        return JSONResponse(status_code=500, content={"error": e.details()})
    return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/v1/health")
def health():
    return {"status": "ok", "auth": "enabled" if auth_enabled(config) else "disabled"}

@app.get("/api/v1/system/summary")
def get_system_summary(_auth=Depends(require_auth)):
    try:
        summary_json = client.get_system_summary()
        return json.loads(summary_json)
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs")
def submit_job(req: SubmitJobRequest, _auth=Depends(require_auth)):
    try:
        if req.bundle_path:
            manifest_json, payloads_bytes = _load_uploaded_bundle(req.bundle_path)
        elif req.manifest_json is not None:
            manifest_json = req.manifest_json
            payloads_bytes = (
                {k: v.encode("utf-8") for k, v in req.payloads.items()}
                if req.payloads
                else {}
            )
        else:
            raise HTTPException(
                status_code=422,
                detail="manifest_json or _bundle_path is required",
            )

        job_id = client.submit_job(manifest_json, payloads_bytes)
        return {"id": job_id, "status": "pending"}
    except HTTPException:
        raise
    except Exception as e:
        return handle_grpc_error(e)


@app.post("/api/v1/bundles/upload")
async def upload_bundle(bundle: UploadFile = File(...), _auth=Depends(require_auth)):
    try:
        if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="bundle must be a .zip file")

        BUNDLE_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        target_dir = Path(tempfile.mkdtemp(prefix="bundle_", dir=BUNDLE_UPLOAD_ROOT))
        archive_path = target_dir / "bundle.zip"
        archive_path.write_bytes(await bundle.read())

        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                destination = _safe_extract_path(target_dir, member.filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source:
                    destination.write_bytes(source.read())

        archive_path.unlink(missing_ok=True)
        bundle_root = _find_bundle_root(target_dir)
        manifest_path = bundle_root / "manifest.json"
        payloads_path = bundle_root / "payloads"

        if not manifest_path.is_file() or not payloads_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail="bundle zip must contain manifest.json and payloads/",
            )

        return {
            "bundle_path": str(bundle_root),
            "manifest": json.loads(manifest_path.read_text()),
        }
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip bundle") from exc


def _load_uploaded_bundle(bundle_path: str) -> tuple[str, Dict[str, bytes]]:
    bundle_root = Path(bundle_path).resolve()
    upload_root = BUNDLE_UPLOAD_ROOT.resolve()
    if not _inside_path(bundle_root, upload_root) or not bundle_root.is_dir():
        raise HTTPException(status_code=400, detail="unknown uploaded bundle")

    manifest_path = bundle_root / "manifest.json"
    payloads_path = bundle_root / "payloads"
    if not manifest_path.is_file() or not payloads_path.is_dir():
        raise HTTPException(status_code=400, detail="invalid uploaded bundle")

    payloads = {}
    for path in payloads_path.rglob("*"):
        if path.is_file():
            payloads[path.relative_to(payloads_path).as_posix()] = path.read_bytes()

    return manifest_path.read_text(), payloads


def _safe_extract_path(root: Path, member_name: str) -> Path:
    member_path = Path(member_name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise HTTPException(status_code=400, detail="bundle contains unsafe paths")

    destination = (root / member_path).resolve()
    if not _inside_path(destination, root.resolve()):
        raise HTTPException(status_code=400, detail="bundle contains unsafe paths")
    return destination


def _find_bundle_root(extracted_root: Path) -> Path:
    if (extracted_root / "manifest.json").is_file():
        return extracted_root

    children = [path for path in extracted_root.iterdir() if path.is_dir()]
    if len(children) == 1 and (children[0] / "manifest.json").is_file():
        return children[0]

    return extracted_root


def _inside_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

@app.get("/api/v1/jobs")
def list_jobs(limit: int = 20, include_terminal: bool = True, _auth=Depends(require_auth)):
    try:
        jobs_json = client.list_jobs(limit, include_terminal)
        return json.loads(jobs_json)
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs:cleanup")
@app.post("/api/v1/jobs/cleanup")
def cleanup_jobs(_auth=Depends(require_auth)):
    try:
        cleared_count = client.clear_jobs()
        return {"cleared_count": cleared_count}
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, _auth=Depends(require_auth)):
    try:
        job_json = client.get_job(job_id)
        return json.loads(job_json)
    except Exception as e:
        return handle_grpc_error(e)


@app.get("/api/v1/jobs/{job_id}/agent-graph")
def get_job_agent_graph(job_id: str, _auth=Depends(require_auth)):
    try:
        details = json.loads(client.get_job(job_id))
        events = [json.loads(event_json) for event_json in client.stream_events(job_id)]
        return _build_agent_graph(job_id, details, events)
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/jobs/{job_id}/events")
def get_job_events(job_id: str, _auth=Depends(require_auth)):
    try:
        events = []
        for event_json in client.stream_events(job_id):
            events.append(json.loads(event_json))
        return {"data": events}
    except Exception as e:
        return handle_grpc_error(e)


@app.get("/api/v1/jobs/{job_id}/dead-letters")
def get_job_dead_letters(job_id: str, _auth=Depends(require_auth)):
    try:
        dead_letters = []
        for event_index, event_json in enumerate(client.stream_events(job_id)):
            event = json.loads(event_json)
            if event.get("type") == "dead_letter":
                dead_letters.append(
                    {
                        "index": len(dead_letters),
                        "event_index": event_index,
                        "agent_id": event.get("agent_id"),
                        "reason": event.get("reason") or event.get("error"),
                        "timestamp": event.get("timestamp"),
                        "message": event.get("message"),
                    }
                )
        return {"job_id": job_id, "data": dead_letters}
    except Exception as e:
        return handle_grpc_error(e)


@app.post("/api/v1/jobs/{job_id}/dead-letters/{index}/replay")
def replay_job_dead_letter(job_id: str, index: int, _auth=Depends(require_auth)):
    raise HTTPException(
        status_code=501,
        detail={
            "error": "dead_letter_replay_not_exposed",
            "job_id": job_id,
            "index": index,
            "message": "core replay is available in-process; gRPC replay will be added to expose it over REST",
        },
    )

@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _auth=Depends(require_auth)):
    try:
        status = client.cancel_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs/{job_id}/pause")
def pause_job(job_id: str, _auth=Depends(require_auth)):
    try:
        status = client.pause_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs/{job_id}/resume")
def resume_job(job_id: str, _auth=Depends(require_auth)):
    try:
        status = client.resume_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/metrics")
def get_metrics(_auth=Depends(require_auth)):
    try:
        summary = json.loads(client.get_system_summary())
        if "metrics" in summary:
            return summary["metrics"]

        jobs = summary.get("jobs", [])
        return {
            "jobs": {
                "total": len(jobs),
                "by_status": _counts(job.get("status", "unknown") for job in jobs),
            },
            "nodes": {"total": len(summary.get("nodes", []))},
            "source": "system_summary",
        }
    except Exception as e:
        return handle_grpc_error(e)


def _counts(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _build_agent_graph(job_id: str, details: Dict[str, Any], events: list[Dict[str, Any]]):
    agents = details.get("agents", []) or []
    job = details.get("job", {}) or {}
    manifest = job.get("topology") or _load_manifest_for_job(job)
    agent_by_id: Dict[str, Dict[str, Any]] = {}

    for agent in agents:
        agent_id = agent.get("agent_id") or agent.get("node_id")
        if agent_id:
            agent_by_id[agent_id] = agent

    for node in manifest.get("nodes", []) if isinstance(manifest, dict) else []:
        node_id = node.get("node_id") or node.get("agent_id")
        if node_id:
            agent_by_id.setdefault(
                node_id,
                {
                    "agent_id": node_id,
                    "agent_type": node.get("agent_type") or "unknown",
                    "type": node.get("type") or "unknown",
                    "status": "declared",
                    "assigned_node": "unassigned",
                    "processed_messages": 0,
                    "mailbox_depth": 0,
                },
            )

    edge_counts: Dict[tuple[str, str, str], Dict[str, Any]] = {}

    for edge in manifest.get("edges", []) if isinstance(manifest, dict) else []:
        source = edge.get("from_node")
        target = edge.get("to_node")
        message_type = edge.get("message_type") or "*"
        if not source or not target:
            continue

        _ensure_graph_agent(agent_by_id, source)
        _ensure_graph_agent(agent_by_id, target)
        key = (source, target, message_type)
        edge_counts.setdefault(
            key,
            {
                "id": edge.get("edge_id") or f"{source}->{target}:{message_type}",
                "source": source,
                "target": target,
                "message_type": message_type,
                "count": 0,
                "last_seen_at": None,
                "source_event": "manifest",
            },
        )

    for event in events:
        message = _event_message_summary(event)
        if not message:
            continue

        source = message.get("from")
        target = message.get("to") or event.get("agent_id")
        message_type = message.get("type") or event.get("type") or "message"

        if not source or not target:
            continue

        _ensure_graph_agent(agent_by_id, source)
        _ensure_graph_agent(agent_by_id, target)
        key = (source, target, message_type)
        existing = edge_counts.setdefault(
            key,
            {
                "id": f"{source}->{target}:{message_type}",
                "source": source,
                "target": target,
                "message_type": message_type,
                "count": 0,
                "last_seen_at": None,
                "source_event": "agent_message_received",
            },
        )
        existing["count"] += 1
        existing["last_seen_at"] = event.get("timestamp") or existing["last_seen_at"]
        if existing.get("source_event") == "manifest":
            existing["source_event"] = "manifest+events"

    for agent in agents:
        source = agent.get("agent_id") or agent.get("node_id")
        outbound_edges = (agent.get("metadata") or {}).get("outbound_edges") or []
        for target in outbound_edges:
            if not source or not target:
                continue
            _ensure_graph_agent(agent_by_id, source)
            _ensure_graph_agent(agent_by_id, target)
            key = (source, target, "*")
            edge_counts.setdefault(
                key,
                {
                    "id": f"{source}->{target}:*",
                    "source": source,
                    "target": target,
                    "message_type": "*",
                    "count": 0,
                    "last_seen_at": None,
                    "source_event": "outbound_edges",
                },
            )

    nodes = [
        {
            "id": agent_id,
            "label": agent_id,
            "agent_type": agent.get("agent_type") or "unknown",
            "type": agent.get("type") or "unknown",
            "status": agent.get("status") or "unknown",
            "assigned_node": agent.get("assigned_node") or "unassigned",
            "processed_messages": agent.get("processed_messages", 0),
            "mailbox_depth": agent.get("mailbox_depth", 0),
        }
        for agent_id, agent in sorted(agent_by_id.items())
    ]

    edges = sorted(edge_counts.values(), key=lambda edge: (edge["source"], edge["target"], edge["message_type"]))

    return {
        "job_id": job_id,
        "graph_id": job.get("graph_id") or (details.get("summary") or {}).get("graph_id"),
        "status": job.get("status") or "unknown",
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "agent_count": len(nodes),
            "edge_count": len(edges),
            "message_count": sum(edge.get("count", 0) for edge in edges),
            "event_count": len(events),
        },
    }


def _load_manifest_for_job(job: Dict[str, Any]) -> Dict[str, Any]:
    manifest_ref = job.get("manifest_ref") or {}
    manifest_path = manifest_ref.get("manifest_path")
    if not manifest_path:
        return {}

    path = Path(manifest_path)
    if not path.is_file():
        return {}

    try:
        manifest = json.loads(path.read_text())
    except Exception:
        logger.exception("Failed to load manifest for graph from %s", manifest_path)
        return {}

    return manifest if isinstance(manifest, dict) else {}


def _event_message_summary(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = event.get("payload")
    if event.get("type") == "agent_message_received" and isinstance(payload, dict):
        return payload

    if event.get("type") in {"backpressure_signal", "delivery_failed", "backpressure_rejected"} and isinstance(payload, dict):
        return payload

    message = event.get("message")
    if isinstance(message, dict):
        envelope = message.get("envelope")
        if isinstance(envelope, dict):
            return envelope
        return message

    return None


def _ensure_graph_agent(agent_by_id: Dict[str, Dict[str, Any]], agent_id: str):
    agent_by_id.setdefault(
        agent_id,
        {
            "agent_id": agent_id,
            "agent_type": "external",
            "type": "message",
            "status": "observed",
            "assigned_node": "unknown",
            "processed_messages": 0,
            "mailbox_depth": 0,
        },
    )


def start():
    logger.info("Starting API server on %s:%s", config.host, config.port)
    uvicorn.run("mn_api.main:app", host=config.host, port=config.port, reload=False)

if __name__ == "__main__":
    start()
