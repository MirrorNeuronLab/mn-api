# MirrorNeuron API Specification

## Purpose

`mn-api` is the HTTP gateway for the local MirrorNeuron runtime. It translates
HTTP and Server-Sent Event interactions into calls to the
MirrorNeuron Python SDK and returns browser- and desktop-consumable responses.
The package installs the `mn-api` service and the `mn-web-ui-server` static/proxy
service.

This specification covers only this repository. Core runtime semantics and
shared Python behavior are external contracts consumed through
`mirrorneuron-python-sdk`.

Launch keeps the compatibility phase identifier `model_install`, but the phase
validates declarations and reports deferred policies only. It must not install
a DMR model or inject a fixed model endpoint. Actual selection, install/reuse,
and gateway routing occur inside the job on first use and surface through Core
runtime model events. RAG and OCR models are specified by their skills at call
time and are therefore absent from launch declarations and progress details.
Explicit model install routes remain eager.

Catalog blueprint loading applies the shared `mn.payloads.v1` contract before
agent rendering or validation. Payload Python dependencies participate in
HostLocal environment preparation, large assets are staged by reference, and
payload model files are packaged locally before normal runtime-model
preparation.

## Owned Surface

The API owns:

- the FastAPI application and `/api/v1` route surface;
- the `/api/v1` HTTP adaptation for stable-job definitions and one-to-many
  execution runs;
- the stateless, URL-bound supervisory MCP adaptation at
  `/api/v1/jobs/{job_id}/mcp` for MCP-enabled blueprint jobs;
- HTTP request parsing, schema validation, authentication, and request limits;
- JSON/problem response shapes and transport-level status mapping;
- run/job progress over snapshots and authenticated, resumable SSE;
- durable group-operation start, status, and resumable SSE event
  adaptation;
- API-side launch coordination, artifact access, and process cleanup helpers;
- API and Web UI server configuration; and
- injection points that make route behavior deterministic in tests.

It does not own workflow scheduling, gRPC runtime semantics, manifest expansion,
model placement algorithms, blueprint domain behavior, or the browser UI.

## Interface Contract

- Every REST path is rooted at `/api/v1`. `/api/v2`, historical aliases,
  runtime-run paths, and WebSocket routes are not mounted.
- `GET /api/v1/health` advertises `api_contract: "mirrorneuron.rest.v1"`.
- REST requests and responses do not contain a transport `version` field.
- The surface includes runtime/system health, blueprints, bundles, jobs, runs,
  schedules/events, deployments, models, services, resources, artifacts, and
  realtime progress.
- Existing model-remote and model-proxy REST paths are compatibility adapters
  over the SDK-owned `$MN_HOME/models/registry.json`; they do not restore the
  removed CLI proxy/manual-remote lifecycle or project legacy ledgers.
- Collection responses contain `items` and an opaque `next_page_token`.
- Streaming endpoints must terminate on completion, error, timeout, or client
  disconnect and must not leak background tasks.
- Workflow-progress polling and streams expose public step dependencies. Hidden
  lowered runtime nodes such as start/end/fork/join nodes are transitively
  projected into source-facing edges and layers at the API boundary.
- Group operations use fixed Core-owned kinds (`cancel_all_jobs`, `clear_jobs`,
  `reconcile_node`, and `drain_node`). Their item events are replayable by
  sequence cursor. `cancellation_pending` is accepted durable work, while
  explicit item failures remain operation failures.
- Blueprint lifecycle terminology matches the CLI: clients create asynchronous
  additions with `POST /blueprints/{blueprint_id}/additions` and removals with
  `POST /blueprints/{blueprint_id}/removals`. The removed blueprint
  `installation` resource is not mounted.
- Blueprint additions are API-owned local operations. Their snapshots and SSE
  events expose monotonic percent, stage, label, and detail fields; terminal
  failures expose sanitized stable codes, retryability, hints, and bounded
  prerequisite issues. A successful addition records the blueprint locally so
  another client-side CLI step is neither required nor permitted as part of the
  HTTP workflow.
- `job_id` is stable and owns configuration, schedules, and shared data;
  `run_id` is the execution/control identity. Batch starts create fresh runs.
  A blueprint-owned Web UI is optional singular Job state: its durable handle
  is served only at `GET /api/v1/jobs/{job_id}/ui` and is shared by every run
  of that Job. Run UI paths are not mounted.
  Only executable `type: service` jobs are single-run: ordinary second starts
  return HTTP 409 Problem Details with code `service_run_exists`, while explicit
  `replace_existing_run` requires a fresh caller-supplied `run_id` and returns
  that run plus optional replaced-run and deferred-cleanup metadata.
  Retry/recovery attempts retain their run ID.
- Archive retains shared data. Data reset and permanent job deletion are
  explicit operations; confirmed deletion is rejected while runs are active.
  Individual run deletion never deletes job data.
- Stable `job_id`, execution `run_id`/`execution_id`, and internal diagnostic
  `runtime_run_id` are separate identities. Clients use only `run_id` in URLs.
- Blueprint launch creates a stable job plus its first run unless an existing
  `job_id` is supplied, and returns both identities. Existing jobs receive the
  freshly prepared manifest and payloads through atomic bundle replacement
  before the new run starts.
  Existing service jobs use a separate `replace_existing_run` field; blueprint
  `force` remains validation bypass and never implies destructive replacement.
- When `owner_node` selects a federated Core, launch preserves that owner through
  asynchronous request normalization and passes the same selected-node handoff
  into SDK preparation before forwarding Job creation. Distributed workflows
  prepare HostLocal Python environments on that owner without changing their
  placement declaration.
- Background output relays poll the execution run ID, which remains separate
  from the durable job ID used for definition paths and launch responses.
- Blueprint launch delegates manifest expansion, config application,
  dependency localization, environment injection, and topology lowering to the
  same SDK preparation path consumed by the CLI.
- Blueprint launch accepts a bounded optional `secret_environment` map. Names
  must be declared by the blueprint through `pass_env`; values are injected
  only into matching executable workers and are excluded from resolved
  configuration, API responses, progress events, and public monitor manifests.
- Blueprint launch persists the SDK's sanitized source-facing monitor manifest
  beside the run identity mapping, matching `mn blueprint run` and preventing
  lowered control nodes from appearing as public workflow steps.
- Blueprint-specific live controls are served by the owning blueprint service.
  `mn-api` does not translate product action routes into runtime messages.
- Stable-job creation may resolve a catalog `blueprint_id`; only API-trusted
  catalog sources or uploaded bundle roots are read from the host filesystem.
  Caller-provided arbitrary host paths are rejected.
- A legacy MCP-enabled catalog Job exposes the read-only tools
  `get_job_profile`, `get_latest_run`, and `get_job_context` through Streamable
  HTTP. A response-enabled Job exposes those tools plus `ask_job`. Tool inputs
  cannot select another Job. Context responses use `mn.mcp.job_context.v1`,
  contain at most 50 evidence records and 256 KiB, and retain the stable
  profile with warnings when latest-run data cannot be read. Never-run,
  running, paused, scheduled-waiting, idle, and archived Jobs remain readable;
  deleted, unknown, and non-enabled Jobs share a sanitized not-found response.
- `ask_job` accepts an 8,000-character question, an optional UUID conversation
  ID, and an optional 128-character request ID. It returns the bounded
  `mn.mcp.job_answer.v1` contract through Core's owner-routed unary query,
  never creates a Run, and has no REST, SSE, or UI chat equivalent.
- The stable supervisory MCP excludes credentials, secret/environment values,
  raw logs, host paths, arbitrary files, and unrestricted artifact bodies. It
  cannot mutate job, run, schedule, approval, or configuration state.
- Legacy Core `mn-job-collaboration` services remain separate, run-scoped peer
  surfaces for blueprints that have not opted into the definition response
  service.

The route definitions and generated OpenAPI document are authoritative for
exact fields and paths. `tests/test_v1_contract.py` protects consumer-visible
behavior. Executable replacement uses `PUT /jobs/{job_id}/bundle` with an
opaque `bundle_id`.

## Errors

Validation and application failures use RFC 9457 Problem Details with stable
error codes. Responses use `application/problem+json` and contain `type`,
`title`, `status`, `detail`, `instance`, `code`, and `request_id`, with bounded
field issues in `errors`. SDK and gRPC failures are normalized before reaching
clients.

Client responses and logs must not expose secrets, authorization values, raw
payloads, tracebacks, or unsanitized internal context. Request and correlation
IDs may be returned for diagnosis.

## Security and Configuration

- `MN_API_TOKEN` enables bearer authentication. Protected HTTP and SSE routes
  plus stable job MCP accept the Authorization header. Credentials are never
  accepted in URL query parameters.
- `MN_API_REQUEST_SIZE_LIMIT_BYTES` bounds declared request body size.
- CORS is disabled unless origins are explicitly configured.
- Artifact and bundle paths must remain within their permitted roots.
- Configuration is loaded through `mn_api.config` and the declared schema.
  Real environment variables override environment-file defaults.
- Sensitive configuration values are redacted based on schema metadata and
  secret-like key names.

The current supported keys and defaults live in `mn_api/config_schema.py` and
are documented in `.env.example` and `README.md`.

## Dependency Boundary

Routes call shared clients and helpers rather than duplicating SDK business
logic. External services are supplied through configured state/dependencies so
tests can use fakes. Importing the package or constructing the app must not
require live Core, Redis, Docker, OpenShell, or network access.

## Compatibility

Changes to paths, methods, required fields, response/event shapes, status codes,
error codes, authentication, or default behavior require focused contract
tests, consumer review, and documentation. This release is an intentional
clean break with no compatibility facade.

## Verification

The repository acceptance gate is:

```bash
python -m ruff check .
python -m pytest
python -m build
```

The test configuration requires at least 85 percent branch coverage. Live
system behavior belongs in cross-repository system tests; this repository's
normal suite must remain deterministic and dependency-injected.
