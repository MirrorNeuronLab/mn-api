# MirrorNeuron API

`mn-api` is the FastAPI REST gateway for MirrorNeuron. It exposes runtime,
blueprint, job, graph, event, metric, deployment, model, service, resource,
and run-artifact endpoints and forwards runtime calls to the core through the
Python SDK gRPC client.

The shared business logic lives in `../mn-python-sdk/mn_sdk`. The CLI and API
are adapters over that SDK: CLI commands render terminal output, while API
routes validate HTTP payloads and return JSON/problem responses.

Blueprint launch accepts blueprint-owned source packages or wheels below
`payloads/skills` and `payloads/agents`, including a bundled agent index. It
streams large assets to the shared blob store and packages declared
`payloads/models` sources in Docker Model Runner before launch.
Manifest expansion, config application, dependency localization, environment
injection, and topology lowering use the SDK's shared manifest-preparation
path, the same path used by `mn blueprint run`.
After a run starts, the API uses the SDK run-store writer to persist the same
public monitor manifest as the CLI, keeping generated control nodes and
internal runtime staff out of the workflow step view.

Blueprint run requests may include `secret_environment`, a bounded map whose
values are treated as secrets by request validation. Every name must be
declared by the selected blueprint through `pass_env`; the API injects each
value only into matching executable workers and omits the values from resolved
configuration and public monitor manifests.

Blueprint launch preserves the `model_install` progress phase for compatibility
but only validates and reports lazy policies. Automatic DMR preparation occurs
inside the submitted job on the first LLM call, or when RAG/OCR skills pass
their own model specifications to the shared SDK wrapper. Skill-owned models
are not blueprint launch declarations. Explicit model-install endpoints remain
eager and unchanged.

## Quick Start

Install locally and run tests:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

OtterDesk and the Web UI consume the same canonical REST and SSE contract.

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

- Capability: unauthenticated `GET /health` returns
  `api_contract: "mirrorneuron.rest.v1"`.
- Jobs: `GET/POST /jobs`, `GET/PATCH/DELETE /jobs/{job_id}`,
  `PUT /jobs/{job_id}/bundle`, `POST /jobs/{job_id}/data-resets`, and the
  read-only Streamable HTTP MCP endpoint at `/jobs/{job_id}/mcp` for eligible
  blueprint jobs.
- Runs: `POST/GET /jobs/{job_id}/runs`,
  `POST /blueprints/{blueprint_id}/runs`, `GET /runs`, and
  `GET/PATCH/DELETE /runs/{run_id}`.
- Run detail: logs, events, resources, human requests, UI, artifacts, outputs,
  snapshots, workflow progress, agent graph, export, and observability all live
  below `/runs/{run_id}`. `runtime_run_id` is diagnostic metadata only.
- Blueprints and bundles: `GET /blueprints`, asynchronous additions at
  `POST /blueprints/{id}/additions`, removals at
  `POST /blueprints/{id}/removals`, validations, catalog refresh/cleanup
  operations, and multipart `POST /bundles` returning an opaque `bundle_id`.
- Scheduling: schedules are created only through
  `POST /jobs/{job_id}/schedules` and are returned with the authoritative job.
- Infrastructure: `/nodes`, `/models`, `/model-remotes`,
  `/model-proxies`, `/services/{name}/resolution`, and `/service-checks`.
- Administrative work: `/operations` and `/operations/{id}`.
- Streams: authenticated, resumable SSE at
  `/runs/{run_id}/events/stream` and
  `/operations/{operation_id}/events/stream`.

Workflow-progress snapshots expose source-facing `edges` and `layers`. When
Core reports a lowered runtime graph, the API projects dependencies through
internal start/end/fork/join nodes so desktop clients can render parallel
branches without reading runtime bundle-cache files.

`job_id` is a persistent configuration and data owner; `run_id` is one
execution. Every manual or scheduled start creates a new Run. There is no
compatibility facade, redirect, host-path request field, or JSON `version`
field.

Collections use `items` and `next_page_token`; clients pass `page_size`
(default 50, maximum 200) and an opaque `page_token`. Persistent resources use
strong ETags. Mutating or deleting jobs, schedules, deployments, model
registrations, and model installations requires `If-Match`. Non-idempotent
POSTs accept `Idempotency-Key`, which first-party clients always set for starts,
dispatches, blueprint additions/removals, and administrative work.

`POST /api/v1/blueprints/{blueprint_id}/runs` creates an ephemeral stable job
before starting the first run unless the body supplies an existing `job_id`.
Responses return a pending Run immediately. Run cleanup never deletes the
stable job's shared data.
Set `owner_node` to a healthy federated Core when the blueprint must be
prepared and executed on that machine; the selected owner is retained through
preflight, bundle preparation, and Job creation.
When an existing `job_id` is supplied, the API installs the freshly prepared
bundle before starting the run; job data, schedules, and prior run history are
preserved.

Legacy blueprints that enable `mcp_collaboration` expose the stable three-tool
Job MCP at `/api/v1/jobs/{job_id}/mcp`. Blueprints that instead declare the
top-level `response_service: {"enabled": true}` expose the same context tools
plus `ask_job(question, conversation_id?, request_id?)`. The responder is
definition-scoped and remains available before the first Run and between Runs;
asking never creates a Run. Context uses `mn.mcp.job_context.v1`, is limited to
256 KiB and 50 evidence records, and omits secrets, environment values, raw
logs, host paths, and unrestricted artifact bodies. Answers use
`mn.mcp.job_answer.v1`, are limited to 64 KiB, and fall back to a deterministic
grounded status summary when the model or Job RAG is unavailable. There is no
REST, SSE, or UI chat surface.

## SDK Usage

Use SDK services directly when building another client:

```python
from mn_sdk import Client, RuntimeService, periodic_schedule

service = RuntimeService(Client())
jobs = service.list_stable_jobs(include_archived=False)
schedule = periodic_schedule(crons=["*/5 * * * *"], name="every-five")
```

Reusable SDK modules added for client parity include resource normalization,
duration parsing, schedule payload builders, deployment policy creation,
runtime service operations, model runtime management, and shared exceptions.

## Operations and errors

Bulk cancellation, job cleanup, node reconciliation, and node drain return a
durable Core operation rather than waiting for every item synchronously. Follow
`GET /operations/{operation_id}/events/stream` to receive replayable SSE
updates and reconnect with `Last-Event-ID`.

Blueprint additions and removals return an API-owned operation immediately.
Poll `GET /operations/{operation_id}` or follow its event stream for the real
`progress.percent`, `progress.stage`, `progress.label`, and `progress.detail`.
Terminal failures include a sanitized `error` with a stable `code`, actionable
`detail` and `hint`, retryability, and bounded prerequisite issues. A successful
addition writes the same local blueprint record consumed by the runtime tools;
clients do not need to invoke `mn blueprint add` separately.

All failures use RFC 9457 `application/problem+json` with `type`, `title`,
`status`, `detail`, `instance`, `code`, and `request_id`; field errors are
bounded in `errors`.

## Details

- [MirrorNeuron Component Guide](../mn-docs/component-guide.md#api)
- [API Reference](../mn-docs/api.md)
- [Environment Variables](../mn-docs/env_variables.md)
- [Security Model](../mn-docs/security.md)

## Notes

- A running MirrorNeuron core is required for live runtime calls.
- Stable job MCP reads require `mn-api` and Core to be reachable, but do not
  require the target job to be running.
- Use `MN_ENV=prod` with `MN_API_TOKEN` when exposing protected endpoints.
- `MN_RUNS_ROOT` controls where run artifacts are read from.
