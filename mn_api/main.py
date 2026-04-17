from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional
import json
import uvicorn
from mn_sdk import Client
import grpc

app = FastAPI(title="MirrorNeuron API", version="1.0")
client = Client()

class SubmitJobRequest(BaseModel):
    manifest_json: str
    payloads: Optional[Dict[str, str]] = {}

def handle_grpc_error(e: Exception):
    if hasattr(e, 'details'):
        return JSONResponse(status_code=500, content={"error": e.details()})
    return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/v1/health")
def health():
    return {"status": "ok"}

@app.get("/api/v1/system/summary")
def get_system_summary():
    try:
        summary_json = client.get_system_summary()
        return json.loads(summary_json)
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs")
def submit_job(req: SubmitJobRequest):
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
def list_jobs(limit: int = 20, include_terminal: bool = True):
    try:
        jobs_json = client.list_jobs(limit, include_terminal)
        return json.loads(jobs_json)
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    try:
        job_json = client.get_job(job_id)
        return json.loads(job_json)
    except Exception as e:
        return handle_grpc_error(e)

@app.get("/api/v1/jobs/{job_id}/events")
def get_job_events(job_id: str):
    try:
        events = []
        for event_json in client.stream_events(job_id):
            events.append(json.loads(event_json))
        return {"data": events}
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        status = client.cancel_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs/{job_id}/pause")
def pause_job(job_id: str):
    try:
        status = client.pause_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        return handle_grpc_error(e)

@app.post("/api/v1/jobs/{job_id}/resume")
def resume_job(job_id: str):
    try:
        status = client.resume_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        return handle_grpc_error(e)

def start():
    uvicorn.run("mn_api.main:app", host="0.0.0.0", port=4001, reload=False)

if __name__ == "__main__":
    start()
