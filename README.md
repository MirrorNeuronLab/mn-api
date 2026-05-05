# MirrorNeuron API

HTTP gateway for the MirrorNeuron runtime.

The API is a FastAPI/Uvicorn service that exposes runtime operations over REST and forwards them to the MirrorNeuron core through the Python SDK gRPC client.

## Features

- Health and runtime summary endpoints.
- Job submission from JSON manifests or uploaded bundle ZIP files.
- Job listing, status, event, graph, metrics, and dead-letter endpoints.
- Job lifecycle controls for cancel, pause, resume, and cleanup.
- Optional bearer-token protection for production mode.
- Request-size and CORS configuration through environment variables.

## Tech Stack

| Area | Tooling |
| --- | --- |
| Runtime | Python 3.9+ |
| Web framework | FastAPI |
| Server | Uvicorn |
| Core client | `mirrorneuron-python-sdk` |
| Packaging | setuptools with setuptools-scm |

## Prerequisites

- Python 3.9 or newer.
- A running MirrorNeuron core reachable over gRPC.
- Redis and any runtime dependencies required by the core deployment.

## Installation

The released-package installer installs this package automatically and exposes `mn-api` on your `PATH`.

Standalone install:

```bash
pip install mirrorneuron-api
```

Developer install:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## Configuration

All runtime configuration uses environment variables.

| Variable | Default | Description |
| --- | --- | --- |
| `MN_ENV` | `dev` | Runtime mode. Use `prod` for protected deployments. |
| `MN_API_HOST` | `localhost` | Bind host for the HTTP server. |
| `MN_API_PORT` | `4001` | Bind port for the HTTP server. |
| `MN_API_TOKEN` | unset | Required when `MN_ENV=prod`. |
| `MN_CORE_HOST` | `localhost` | Core host used to build the default gRPC target. |
| `MN_GRPC_TARGET` | unset | Full core gRPC target. Takes precedence over `MN_CORE_GRPC_TARGET`. |
| `MN_CORE_GRPC_TARGET` | unset | Fallback full core gRPC target. |
| `MN_GRPC_TIMEOUT_SECONDS` | `10` | SDK call timeout. Use `0` or `none` to disable. |
| `MN_API_REQUEST_SIZE_LIMIT_BYTES` | `5242880` | Maximum request body size. |
| `MN_API_CORS_ALLOW_ORIGINS` | unset | Comma-separated CORS allowlist. |
| `MN_API_LOG_PATH` | `~/.mn/logs/api.log` | API log file path. |
| `MN_LOG_LEVEL` | package default | Log level used by shared logging setup. |
| `MN_LOG_MAX_BYTES` | package default | Rotating log size limit. |
| `MN_LOG_BACKUP_COUNT` | package default | Rotating log backup count. |

Protected endpoints require:

```http
Authorization: Bearer <MN_API_TOKEN>
```

## Running

```bash
mn-api
```

The service listens on `http://localhost:4001` by default.

Example production-style local run:

```bash
MN_ENV=prod \
MN_API_TOKEN=replace-me \
MN_GRPC_TARGET=localhost:50051 \
mn-api
```

## API Endpoints

Base path: `/api/v1`

| Method | Route | Description |
| --- | --- | --- |
| `GET` | `/health` | Service health check. |
| `GET` | `/system/summary` | Runtime hardware and pool summary. |
| `GET` | `/metrics` | Runtime metrics summary. |
| `POST` | `/jobs` | Submit a workflow from a JSON manifest. |
| `POST` | `/bundles/upload` | Upload and submit a bundle ZIP. |
| `GET` | `/jobs` | List jobs. |
| `DELETE` | `/jobs` | Clear jobs. |
| `GET` | `/jobs/{job_id}` | Fetch job status. |
| `GET` | `/jobs/{job_id}/graph` | Fetch agent graph details. |
| `GET` | `/jobs/{job_id}/events` | Fetch job events. |
| `GET` | `/jobs/{job_id}/dead-letters` | Inspect dead-letter events. |
| `POST` | `/jobs/{job_id}/dead-letters/{index}/replay` | Replay a dead-letter event. |
| `POST` | `/jobs/{job_id}/cancel` | Cancel a job. |
| `POST` | `/jobs/{job_id}/pause` | Pause a job. |
| `POST` | `/jobs/{job_id}/resume` | Resume a job. |

Example health check:

```bash
curl http://localhost:4001/api/v1/health
```

Example authenticated request:

```bash
curl \
  -H "Authorization: Bearer $MN_API_TOKEN" \
  http://localhost:4001/api/v1/system/summary
```

## Testing

```bash
python3 -m pytest -q
```

## Deployment

The recommended path is the released-package installer in `mn-deploy`, which installs the API from PyPI alongside the CLI, SDK, Web UI, and core OTP release.

For custom deployments:

1. Install `mirrorneuron-api`.
2. Start the MirrorNeuron core.
3. Set `MN_GRPC_TARGET`.
4. Set `MN_ENV=prod` and `MN_API_TOKEN` when exposing the API outside a trusted local environment.
5. Run `mn-api` behind your process manager or service supervisor.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `MN_API_TOKEN` error on startup | `MN_ENV=prod` requires `MN_API_TOKEN`. |
| API starts but runtime calls fail | Confirm the core is running and `MN_GRPC_TARGET` points to it. |
| Browser requests are blocked | Set `MN_API_CORS_ALLOW_ORIGINS` for the Web UI origin. |
| Bundle uploads fail | Check `MN_API_REQUEST_SIZE_LIMIT_BYTES` and bundle ZIP contents. |

## Contributing

Keep API changes aligned with the Python SDK and CLI command surface. Add tests for new routes, request validation, and error handling.

## License

MIT.
