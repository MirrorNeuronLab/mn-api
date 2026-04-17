from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional
import json
import uvicorn
from mn_sdk import Client

app = FastAPI(title="MirrorNeuron API", version="1.0")
client = Client()


class SubmitJobRequest(BaseModel):
    manifest_json: str
    payloads: Optional[Dict[str, str]] = {}  # Base64 or raw string for simplified API


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/system/summary")
def get_system_summary():
    try:
        summary_json = client.get_system_summary()
        return json.loads(summary_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/jobs")
def list_jobs(limit: int = 20, include_terminal: bool = True):
    try:
        jobs_json = client.list_jobs(limit, include_terminal)
        return json.loads(jobs_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    try:
        job_json = client.get_job(job_id)
        return json.loads(job_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        status = client.cancel_job(job_id)
        return {"status": status, "job_id": job_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def start():
    uvicorn.run("mn_api.main:app", host="0.0.0.0", port=4001, reload=False)


if __name__ == "__main__":
    start()
