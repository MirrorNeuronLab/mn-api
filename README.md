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

All overrides use `MIRROR_NEURON_` env vars:

- `MIRROR_NEURON_ENV=prod` requires `MIRROR_NEURON_API_TOKEN`.
- `MIRROR_NEURON_API_HOST`, `MIRROR_NEURON_API_PORT` control binding; default `localhost:4001`.
- `MIRROR_NEURON_CORE_HOST` controls the default core gRPC host; default `localhost`.
- `MIRROR_NEURON_GRPC_TARGET` or `MIRROR_NEURON_CORE_GRPC_TARGET` can override the full core gRPC target.
- `MIRROR_NEURON_GRPC_TIMEOUT_SECONDS` controls SDK calls.
- `MIRROR_NEURON_API_REQUEST_SIZE_LIMIT_BYTES` bounds request bodies.
- `MIRROR_NEURON_API_CORS_ALLOW_ORIGINS` is a comma-separated allowlist.

Protected endpoints accept `Authorization: Bearer <MIRROR_NEURON_API_TOKEN>`.

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
