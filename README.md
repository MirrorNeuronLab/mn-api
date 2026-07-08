# MirrorNeuron API

`mn-api` is the FastAPI REST gateway for MirrorNeuron. It exposes runtime,
blueprint, job, graph, event, metric, deployment, model, service, resource,
and run-artifact endpoints and forwards runtime calls to the core through the
Python SDK gRPC client.

The shared business logic lives in `../mn-python-sdk/mn_sdk`. The CLI and API
are adapters over that SDK: CLI commands render terminal output, while API
routes validate HTTP payloads and return JSON/problem responses.

## Quick Start

Install locally and run tests:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

For OtterDesk consumer compatibility checks covering async blueprint launch,
REST launch progress, and `WS /realtime` launch-progress events, see the
MirrorNeuron API Compatibility Tests section in
`../otterdesk-desktop-app/README.md`.

Start the local API:

```bash
mn-api
```

Default local URL:

```text
http://localhost:54001
```

## Configuration

Configuration is loaded by `mn_api.config` and shared by API, web UI server,
and child CLI/runtime processes. Real environment variables always override
values from `.env` files. Loading order is:

```text
real environment variables
> .env.${MN_ENV}
> .env
> safe built-in defaults
```

If `MN_ENV` is unset it defaults to `dev`. `MN_ENV=development` loads
`.env.dev`; `MN_ENV=prod` and `MN_ENV=production` load `.env.prod` when that
file exists. Production does not require any `.env` file.

Development example:

```bash
export MN_ENV=dev
cp .env.example .env.dev
mn-cli ...
```

Test example:

```bash
export MN_ENV=test
mn-cli ...
```

Production example:

```bash
export MN_ENV=production
export MN_HOME=/var/lib/mirrorneuron
export MN_LOG_LEVEL=info
export MN_API_HOST=0.0.0.0
export MN_API_PORT=8080
export MN_API_TOKEN=replace-with-secret
mn-api
```

Keep real `.env` files local. `.env.example` contains placeholders only and is
safe to commit.

## Endpoint Summary

All paths below are under `/api/v1`.

- Health/runtime: `GET /health`, `GET /runtime/status`, `GET /system/summary`, `GET /metrics`
- Jobs: `POST /jobs`, `GET /jobs`, `GET /jobs/{job_id}`, `POST /jobs/{job_id}/cancel`, `POST /jobs/{job_id}/pause`, `POST /jobs/{job_id}/resume`, `POST /jobs/cleanup`
- Job recovery: `GET /jobs/{job_id}/dead-letters`, `POST /jobs/{job_id}/backup`, `POST /jobs/restore`
- Schedules/events: `POST /schedules`, `POST /schedules/periodic`, `POST /schedules/delayed`, `GET /schedules`, `PATCH /schedules/{schedule_id}`, `POST /schedules/{schedule_id}/dispatch`, `POST /events`, `GET /events`
- Triggers: `POST /triggers`, `GET /triggers`, `DELETE /triggers/{schedule_id}`
- Deployments: `POST /deployments`, `GET /deployments`, `GET /deployments/{id_or_key}`, `POST /deployments/{id_or_key}/promote`, `POST /deployments/{id_or_key}/rollback`, `POST /deployments/{id_or_key}/pause`, `POST /deployments/{id_or_key}/resume`, `POST /deployments/{id_or_key}/fail`
- Nodes/resources: `GET /resource`, `POST /resource`, `POST /nodes/{node_name}/reconcile`, `POST /nodes/{node_name}/drain`, `POST /nodes/{node_name}/undrain`, `POST /nodes/{node_name}/maintenance`
- Services: `GET /services`, `GET /services/{name}/resolve`
- Models: `GET /models`, `GET /models/catalog`, `GET /models/{model_id}`, `POST /models/{model_id}/install`, `POST /models/{model_id}/update`, `DELETE /models/{model_id}`, `GET /models/{model_id}/doctor`, `POST /models/{model_id}/benchmark`
- Blueprints/runs/bundles: `GET /blueprints`, async `POST /blueprints/{blueprint_id}/runs`, async `POST /blueprints/launch/runs`, `GET /blueprints/launch/progress/{progress_id}`, `WS /realtime` topic `launch_progress:{progress_id}`, `POST /bundles/upload`, plus `/runs/{run_id}/...` artifact, UI, event, log, human-response, and observability routes.

## SDK Usage

Use SDK services directly when building another client:

```python
from mn_sdk import Client, RuntimeService, periodic_schedule

service = RuntimeService(Client())
jobs = service.list_jobs(limit=20)
schedule = periodic_schedule(crons=["*/5 * * * *"], name="every-five")
```

Reusable SDK modules added for client parity include resource normalization,
duration parsing, schedule payload builders, deployment policy creation,
runtime service operations, model runtime management, and shared exceptions.

## Details

- [MirrorNeuron Component Guide](../mn-docs/component-guide.md#api)
- [API Reference](../mn-docs/api.md)
- [Environment Variables](../mn-docs/env_variables.md)
- [Security Model](../mn-docs/security.md)

## Notes

- A running MirrorNeuron core is required for live runtime calls.
- Use `MN_ENV=prod` with `MN_API_TOKEN` when exposing protected endpoints.
- `MN_RUNS_ROOT` controls where run artifacts are read from.
