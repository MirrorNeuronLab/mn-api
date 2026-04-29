from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional
import json
import uvicorn
from mn_sdk import Client
import grpc
from mn_api.config import ApiConfig, auth_enabled
from mn_api.logging_config import configure_logging

config = ApiConfig.from_env()
logger = configure_logging()
app = FastAPI(title="MirrorNeuron API", version="1.0")
client = Client(target=config.grpc_target, timeout=config.grpc_timeout_seconds)

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
    manifest_json: str
    payloads: Optional[Dict[str, str]] = {}

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
        payloads_bytes = (
            {k: v.encode("utf-8") for k, v in req.payloads.items()}
            if req.payloads
            else {}
        )
        job_id = client.submit_job(req.manifest_json, payloads_bytes)
        return {"id": job_id, "status": "pending"}
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/jobs")
def list_jobs(limit: int = 20, include_terminal: bool = True, _auth=Depends(require_auth)):
    try:
        jobs_json = client.list_jobs(limit, include_terminal)
        return json.loads(jobs_json)
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, _auth=Depends(require_auth)):
    try:
        job_json = client.get_job(job_id)
        return json.loads(job_json)
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


def start():
    logger.info("Starting API server on %s:%s", config.host, config.port)
    uvicorn.run("mn_api.main:app", host=config.host, port=config.port, reload=False)

if __name__ == "__main__":
    start()
