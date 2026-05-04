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

## Configuration

All overrides use `MN_` env vars:

- `MN_ENV=prod` requires `MN_API_TOKEN`.
- `MN_API_HOST`, `MN_API_PORT` control binding; default `localhost:4001`.
- `MN_CORE_HOST` controls the default core gRPC host; default `localhost`.
- `MN_GRPC_TARGET` or `MN_CORE_GRPC_TARGET` can override the full core gRPC target.
- `MN_GRPC_TIMEOUT_SECONDS` controls SDK calls.
- `MN_API_REQUEST_SIZE_LIMIT_BYTES` bounds request bodies.
- `MN_API_CORS_ALLOW_ORIGINS` is a comma-separated allowlist.

Protected endpoints accept `Authorization: Bearer <MN_API_TOKEN>`.

## Endpoints

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/v1/health` | Service health check |
| `GET` | `/api/v1/system/summary` | Cluster hardware and pool state |
| `GET` | `/api/v1/metrics` | Runtime metrics summary |
| `POST`| `/api/v1/jobs` | Submit a new workflow via JSON |
| `GET` | `/api/v1/jobs` | List recent workflows |
| `GET` | `/api/v1/jobs/{job_id}` | Fetch workflow status |
| `GET` | `/api/v1/jobs/{job_id}/events` | Fetch job events |
| `GET` | `/api/v1/jobs/{job_id}/dead-letters` | Inspect dead-letter events |
| `POST`| `/api/v1/jobs/{job_id}/cancel`| Cancel a running job |
