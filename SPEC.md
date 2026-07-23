# MirrorNeuron API Specification

## Purpose

`mn-api` is the HTTP gateway for the local MirrorNeuron runtime. It translates
HTTP, Server-Sent Events, and WebSocket interactions into calls to the
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

## Owned Surface

The API owns:

- the FastAPI application and `/api/v1` route surface;
- the `/api/v2` HTTP adaptation for stable-job definitions and one-to-many
  execution runs;
- HTTP request parsing, schema validation, authentication, and request limits;
- JSON/problem response shapes and transport-level status mapping;
- run/job progress over polling, SSE, and WebSockets;
- versioned durable group-operation start, status, and resumable SSE event
  adaptation;
- API-side launch coordination, artifact access, and process cleanup helpers;
- API and Web UI server configuration; and
- injection points that make route behavior deterministic in tests.

It does not own workflow scheduling, gRPC runtime semantics, manifest expansion,
model placement algorithms, blueprint domain behavior, or the browser UI.

## Interface Contract

- Existing application routes are rooted at `/api/v1`; stable job/run lifecycle
  routes are rooted at `/api/v2`.
- Ordinary mapping responses receive top-level `version: 1` when a route did
  not already provide a version.
- The surface includes runtime/system health, blueprints, bundles, jobs, runs,
  schedules/events, deployments, models, services, resources, artifacts, and
  realtime progress.
- Established colon and path aliases are compatibility contracts where both are
  registered, such as `services:check` and `services/check`.
- Streaming endpoints must terminate on completion, error, timeout, or client
  disconnect and must not leak background tasks.
- Group operations use fixed Core-owned kinds (`cancel_all_jobs`, `clear_jobs`,
  `reconcile_node`, and `drain_node`). Their item events are replayable by
  sequence cursor. `cancellation_pending` is accepted durable work, while
  explicit item failures remain operation failures.
- In v2, `job_id` is stable and owns configuration, schedules, and shared data;
  `run_id` is the execution/control identity. Starting or dispatching a job
  creates a fresh run, while retry/recovery attempts retain their run ID.
- Archive retains shared data. Data reset and permanent job deletion are
  explicit operations; confirmed deletion is rejected while runs are active.
  Individual run deletion never deletes job data.
- The v1 `/jobs/{id}` contract remains execution-oriented and maps its old
  identifier to a run. Historical terminal records remain readable without
  forcing a rewrite.
- Blueprint launch creates a stable job plus its first run unless an existing
  `job_id` is supplied, and returns both identities.
- Blueprint-specific live controls are served by the owning blueprint service.
  `mn-api` does not translate product action routes into runtime messages.
- Stable-job creation may resolve a catalog `blueprint_id`; only API-trusted
  catalog sources or uploaded bundle roots are read from the host filesystem.
  Caller-provided arbitrary host paths are rejected.

The route definitions and generated OpenAPI document are authoritative for
exact fields and paths. `tests/test_api_contract.py` and route-specific tests
protect consumer-visible behavior.

## Errors

Validation and application failures use structured responses with stable error
codes. Problem responses use `application/problem+json` and contain version,
type, title, status, detail, and error fields, with validation issues when
applicable. SDK and gRPC failures are normalized before reaching clients.

Client responses and logs must not expose secrets, authorization values, raw
payloads, tracebacks, or unsanitized internal context. Request and correlation
IDs may be returned for diagnosis.

## Security and Configuration

- `MN_API_TOKEN` enables bearer authentication. Protected HTTP routes accept
  the Authorization header; protected WebSockets accept that header or the
  supported token query parameter.
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
error codes, authentication, or default behavior are public contract changes.
They require focused contract tests, consumer-impact review, and documentation.
Additive optional fields are acceptable only when existing clients remain
valid. A deliberately incompatible surface must use a new interface version.

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
