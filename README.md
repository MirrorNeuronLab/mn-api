# MirrorNeuron API

The RESTful HTTP Gateway for the MirrorNeuron distributed runtime system.

## Architecture
Built with FastAPI and Uvicorn, this component acts as an HTTP abstraction over the core `mn-python-sdk` gRPC Client. It allows external microservices, web dashboards, or non-Python applications to easily interact with the MirrorNeuron cluster without speaking Protobuf.

## Installation
*Note: This API is installed automatically and symlinked globally as `mn-api` by the MirrorNeuron `install.sh` script.*

```bash
pip install mn-api
```

## Running the Server

```bash
mn-api
```
This runs the Uvicorn server on port `4001` locally.

## Endpoints

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/v1/health` | Service health check |
| `GET` | `/api/v1/system/summary` | Cluster hardware and pool state |
| `POST`| `/api/v1/jobs` | Submit a new workflow via JSON |
| `GET` | `/api/v1/jobs` | List recent workflows |
| `GET` | `/api/v1/jobs/{job_id}` | Fetch workflow status |
| `POST`| `/api/v1/jobs/{job_id}/cancel`| Cancel a running job |