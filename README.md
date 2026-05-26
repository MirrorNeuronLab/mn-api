# MirrorNeuron API

HTTP gateway for the MirrorNeuron runtime.

The API is a FastAPI/Uvicorn service that exposes runtime operations over REST and forwards them to the MirrorNeuron core through the Python SDK gRPC client.

## Features

- Health and runtime summary endpoints.
- Blueprint catalog list/detail/install/run endpoints, including category facets and category filtering for desktop Worker Hub clients.
- Job submission from JSON manifests or uploaded bundle ZIP files.
- Job listing, status, event, graph, metrics, and dead-letter endpoints.
- Job lifecycle controls for cancel, pause, resume, and cleanup.
- Optional bearer-token protection for production mode.
- Request-size and CORS configuration through environment variables.

## Tech Stack

| Area | Tooling |
| --- | --- |
| Runtime | Python 3.11+ |
| Web framework | FastAPI |
| Server | Uvicorn |
| Core client | `mirrorneuron-python-sdk` |
| Packaging | setuptools with setuptools-scm |

## Prerequisites

- Python 3.11 or newer.
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
| `MN_API_HOST` | `~/.mn/docker-compose.env`, then `localhost` | Bind host for the HTTP server. |
| `MN_API_PORT` | `~/.mn/docker-compose.env`, then `54001` | Bind port for the HTTP server. |
| `MN_API_TOKEN` | unset | Required when `MN_ENV=prod`. |
| `MN_CORE_HOST` | `localhost` | Core host used to build the default gRPC target. |
| `MN_GRPC_TARGET` | `~/.mn/docker-compose.env` when present | Full core gRPC target. Takes precedence over `MN_CORE_GRPC_TARGET`. |
| `MN_CORE_GRPC_TARGET` | `~/.mn/docker-compose.env` when present | Fallback full core gRPC target. |
| `MN_GRPC_TIMEOUT_SECONDS` | `10` | SDK call timeout. Use `0` or `none` to disable. |
| `MN_GRPC_AUTH_TOKEN` | `~/.mn/docker-compose.env`, then `~/.mn/grpc_auth.token` when present | Bearer token sent from `mn-api` to the core gRPC service; falls back to the legacy `~/.mirror_neuron/grpc_auth.token` during migration. |
| `MN_MIRROR_NEURON_GRPC_ADMIN_TOKEN` | `~/.mn/docker-compose.env`, then `~/.mn/grpc_admin.token` when present | Admin token sent from `mn-api` to destructive core gRPC calls such as job cleanup; falls back to the legacy `~/.mirror_neuron/grpc_admin.token` during migration. |
| `MN_RUNS_ROOT` | `~/.mn/runs` | Shared run-artifact filesystem used by `/runs/{run_id}/...` endpoints for result JSON, final artifacts, Markdown, PDFs, logs, events, and resources. |
| `MN_API_REQUEST_SIZE_LIMIT_BYTES` | `5242880` | Maximum request body size. |
| `MN_API_CORS_ALLOW_ORIGINS` | unset | Comma-separated CORS allowlist. |
| `MN_BLUEPRINT_REPO` | `https://github.com/MirrorNeuronLab/mn-blueprints.git` | Blueprint catalog repository used by `/blueprints` endpoints. OtterDesk overrides this to its co-worker catalog. |
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

The service listens on `http://localhost:54001` by default.

Example production-style local run:

```bash
MN_ENV=prod \
MN_API_TOKEN=replace-me \
MN_GRPC_TARGET=localhost:55051 \
mn-api
```

## API Endpoints

Base path: `/api/v1`

| Method | Route | Description |
| --- | --- | --- |
| `GET` | `/health` | Service health check. |
| `GET` | `/system/summary` | Runtime hardware and pool summary. |
| `GET` | `/metrics` | Runtime metrics summary. |
| `GET` | `/resource` | Core resource totals and configured CPU/GPU/memory/disk limits. |
| `POST`/`PUT` | `/resource` | Set CPU/GPU/memory/disk limits to `25`, `50`, `75`, or `100` percent. |
| `GET` | `/blueprints` | List normalized blueprint catalog entries and category facets from `MN_BLUEPRINT_REPO`. Supports `?category=<name-or-slug>`. |
| `GET` | `/blueprints/{blueprint_id}` | Fetch one normalized blueprint. |
| `POST` | `/blueprints/{blueprint_id}/install` | Validate/cache a blueprint bundle for local use. |
| `POST` | `/blueprints/{blueprint_id}/validate` | Run the blueprint manifest input validation rules. |
| `POST` | `/blueprints/{blueprint_id}/runs` | Prepare and submit a blueprint bundle, returning `job_id` and `run_id`. |
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

Validation endpoints return `validation.report/v1` payloads with legacy `errors` strings plus structured `issues` for UI rendering. Run and submit failures caused by invalid inputs return `422 application/problem+json`; unmet runtime requirements return `412 application/problem+json`.

Example health check:

```bash
curl http://localhost:54001/api/v1/health
```

Example authenticated request:

```bash
curl \
  -H "Authorization: Bearer $MN_API_TOKEN" \
  http://localhost:54001/api/v1/system/summary
```

Example blueprint category filter:

```bash
curl "http://localhost:54001/api/v1/blueprints?category=finance"
```

The response includes normalized blueprint entries with `category` and `category_slug`, plus a `categories` list with `{name, slug, count}` facet metadata.

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
