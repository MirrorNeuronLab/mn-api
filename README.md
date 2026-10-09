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
Local-only blueprints with a host OS requirement prepare native Python workers
and a separate Core supervision proxy. The API requires the native SDK service's
`mn.native.host-python.v1` response before submission. Its SDK `local-source`
extra preserves SCM versions and extras on staged dependencies.
The API requires SDK `>=1.3.58.dev46,<2` for this native execution contract.
After a run starts, the API uses the SDK run-store writer to persist the same
public monitor manifest as the CLI, keeping generated control nodes and
internal runtime staff out of the workflow step view.
Run workflow-progress reads that saved execution manifest before the stable Job
definition and replays the recent runtime history needed to reconstruct its
step state. Active progress exposes a failure only when the run has failed.
Run file and event reads resolve the canonical `run_data_ref` before a final
result exists. The SDK verifies the exact submission/run directory and its saved
identity before files are read; no legacy local mapping is required. Live
streams read those same saved events while retaining the
public execution ID for progress and control.
For durable Job runs, it starts the shared host output relay even when the
definition and configuration are unchanged. The relay waits for the terminal
file inventory to replicate, copies the declared output folder, and records
delivery status separately from run completion. A host reconciliation loop also
starts missing relays for completed runs dispatched directly by Core schedules.

For older runs whose owner is no longer visible to Core, run detail, workflow
progress, and events can be read from a mapped replicated submission. A shared
record that still says `running` is presented as `unknown` rather than as a
live run.
Human review requests use the mapped submission's human-event ledger when one
exists. An authenticated response to a pending request is appended there, so a
completed blueprint review can be answered from OtterDesk even when its report
is stored in shared outputs. Duplicate or unknown request IDs return HTTP 409.

Blueprint run requests may include `secret_environment`, a bounded map whose
values are treated as secrets by request validation. Every name must be
declared by the selected blueprint through `pass_env`; the API injects each
value only into matching executable workers and omits the values from resolved
configuration and public monitor manifests.

Blueprint launch preserves the `model_install` progress phase for compatibility
and uses it as a blocking readiness gate. Every declared DMR model is selected,
installed or reused, and published through LiteLLM before the job is submitted.
Before that gate, launch applies the SDK input validator used by blueprint
validation and returns HTTP 422 for missing required inputs, without preparing
runtime resources or submitting a job.
The shared SDK still prepares dynamically requested RAG/OCR skill models on
first use, and also rechecks a declared model if it is removed after launch.
Explicit model-install endpoints remain eager.

`GET /api/v1/models` includes the full catalog by default. Each model's
`default` boolean marks membership in the configured default/fallback chain;
`?installed_only=true` limits the collection to installed models. Prepare the
configured default with `PUT /api/v1/models/default/installation` and an empty
JSON body (`{}`). Installation selects a compatible local or cluster node,
uses catalog fallbacks, and preserves the configured default policy. The
endpoint returns `202` and an operation URL for polling or SSE; it supports
`Idempotency-Key` and the existing backend/context/force request options.

Catalog records identify managed delivery with `source: "dmr"` or
`source: "docker"`. NVIDIA Docker models use the same SDK preparation and
gateway synchronization. For Cosmos3 Nano Reasoner, send `{}` to
`PUT /api/v1/models/cosmos3-nano-reasoner:1.7/installation`. The selected
Linux NVIDIA owner's native service must have `NGC_API_KEY` or
`NGC_CLI_API_KEY` in its environment; the HTTP body does not accept secrets.
See [Docker model setup](../mn-python-sdk/docs/docker-models.md).

## Quick Start

### Observe a long submission

Send a unique `X-Launch-Progress-ID` header with `POST /api/v1/jobs` or
`POST /api/v1/blueprints/{blueprint_id}/runs`. While that request is pending,
poll `GET /api/v1/launch-progress/{progress_id}` from another connection, using
the same bearer authentication as other API requests. Use the same progress ID
when replaying a request with its `Idempotency-Key`.

The snapshot contains `status`, `completed`, `current_phase`, `latest`, `phases`,
and up to 200 recent `events`. It starts as `pending` until the API records work.
Preparation reports dependency resolution, model import, sandbox images, payload
staging, Python environments, and Docker worker readiness. Long stages refresh
their detail every ten seconds with elapsed time. Elapsed time is not a
completion estimate. No build logs, configuration, or secret values are added.

Submission keeps its existing synchronous response and status code. Progress is
observational: disconnecting a poll does not cancel submission, and a failed
request status does not prove Core rejected the job. Check the original response
and job state before retrying an uncertain submission.

### Install and run

Requires `mirrorneuron-python-sdk>1.3,<2.0`.

Install locally and run tests:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

OtterDesk and the Web UI consume the same canonical REST and SSE contract.
OtterDesk node removal uses `DELETE /api/v1/nodes/{node_id}`, which forwards to
the Core federated-peer removal contract.

Start the local API:

```bash
mn-api
```

Default local URL:

```text
http://localhost:54001
```

## Configuration

Configuration is loaded through the source-compatible `mn_api.config` facade,
while dotenv parsing, profile normalization, shared defaults, typed parsers,
redaction, and bootstrapping are owned by `mn_sdk.config`. API-only HTTP/Web UI
keys are composed with that SDK schema. Loading order is:

```text
real environment variables
> .env.${MN_ENV}
> .env
> safe built-in defaults
```

If `MN_ENV` is unset it defaults to `dev`. `MN_ENV=development` loads
`.env.dev`; `MN_ENV=prod` and `MN_ENV=production` load `.env.prod` when that
file exists. Production does not require any `.env` file. An explicitly blank
process-environment value overrides the corresponding dotenv value.

Set `MN_MODEL_CATALOG_PATH` in `.env` to select the final operator model catalog.
That catalog contains semantic defaults and fallback links; the API defines no
physical built-in model list or separate preferred/fallback variables.

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

`GET /api/v1/blueprints` and `GET /api/v1/blueprints/{id}` include the SDK's
declared `skills` list with `{name, version_constraint}` entries and the explicit
`air-gapped` flag. An empty skills list means no skill dependencies were declared;
these are requirements, not installed versions or per-run invocation history.
Upgrade the shared SDK with this API to expose this review metadata.

All paths below are under `/api/v1`.

- Capability: unauthenticated `GET /health` returns
  `api_contract: "mirrorneuron.rest.v1"`.
- Jobs: `GET/POST /jobs`, `GET/PATCH/DELETE /jobs/{job_id}`,
  `PUT /jobs/{job_id}/bundle`, `POST /jobs/{job_id}/data-resets`, and the
  read-only Streamable HTTP MCP endpoint at `/jobs/{job_id}/mcp` for eligible
  blueprint jobs. Workflow shape is available at
  `GET /jobs/{job_id}/workflow/{definition|latest-run}/{dag|steps}`.
- Runs: `POST/GET /jobs/{job_id}/runs` (catalog Jobs may include
  `config_overrides`; the API prepares the updated definition before launch),
  asynchronous `POST /jobs/{job_id}/run-operations` with a required
  `Idempotency-Key` and progress at `/operations/{id}/events/stream`,
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
  `Idempotency-Key` is forwarded to Core and replays an identical create request.
  Schedule detail read and update endpoints are not yet available; clients must
  not treat a missing schedule detail response as proof that Core deleted it.
- Infrastructure: `/nodes`, `/models`, `/model-remotes`,
  `/model-proxies`, `/services/{name}/resolution`, and `/service-checks`.
- Administrative work: `/operations` and `/operations/{id}`.
- Streams: authenticated, resumable SSE at
  `/runs/{run_id}/events/stream` and
  `/operations/{operation_id}/events/stream`.

`mn-web-ui-server` also owns a local-only job UI proxy at
`/job-ui-proxy/{job_id}/{port}/...`. It resolves the durable
`/jobs/{job_id}/ui` handle through the authenticated API, then forwards only
the dashboard host and explicitly declared companion ports recorded for that
job while the handle is running. Paused, stopped, cancelled, and failed
services are rejected instead of forwarding to an unavailable upstream. It is
not a public API route or a general-purpose network proxy.
Every UI read verifies the exact page and same-origin scripts/stylesheets from
the proxy host using `mn-python-sdk-web-ui`. A registered service or an old ready
receipt alone is insufficient. Unreachable running handles return `starting`
with `metadata.readiness.ready: false`; the next read can confirm recovery.
The proxy refuses unready handles. Live video bytes are forwarded promptly,
without waiting for a full 64 KiB buffer or for the stream to end.
The desktop defaults to `dom-ready`. A blueprint can select `did-finish-load`
through the Web UI SDK claim's `load_event` parameter (or Core service
`meta.load_event`). Registry projections preserve claimed load policy only for
the same current page; they never adopt a stale claim's address or lifecycle.
Job UI reads prefer the Web UI skill's cross-node handle under shared storage
and fall back to the host-local job-data handle. This lets a DockerWorker on a
federated owner publish its OS-selected listener while the browser continues to
use only the submit host's `/jobs/{job_id}/ui` route.

Job workflow `dag` responses contain logical step nodes, dependency edges,
and layers. `steps` responses contain each step and its agents. The
`definition` view reads the saved Job, including before its first run; the
`latest-run` view includes steps discovered during the most recent run and
returns 404 when no run exists. Jobs without logical steps return empty shapes.
Run workflow-progress snapshots and run event-stream snapshots contain only
progress data; clients combine them with the Job shape views for display.

`job_id` is a persistent configuration and data owner; `run_id` is one
execution. Every manual or scheduled start creates a new Run. There is no
compatibility facade, redirect, host-path request field, or JSON `version`
field.

For catalog-backed Jobs, `POST /api/v1/jobs/{job_id}/runs` also prepares the
run-scoped output-copy state and starts the background output relay. This keeps
shared-output materialization identical to a direct blueprint launch whether
or not the request contains configuration overrides.

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

Blueprints that enable `response_service` expose the stable context-and-activity
Job MCP at `/api/v1/jobs/{job_id}/mcp`. Blueprints that instead declare the
top-level `response_service: {"enabled": true}` expose the same context tools
plus `ask_job(question, conversation_id?, request_id?)`. All variants expose
`watch_job_activity(after_event_id?, wait_seconds?)`. The responder is
definition-scoped and remains available before the first Run and between Runs;
asking never creates a Run. Context uses `mn.mcp.job_context.v1`, is limited to
256 KiB and 50 evidence records, and omits secrets, environment values, raw
logs, host paths, and unrestricted artifact bodies. Answers use
`mn.mcp.job_answer.v1`, are limited to 64 KiB, and fall back to a deterministic
grounded status summary when the model or Job RAG is unavailable. There is no
REST, SSE, or UI chat surface.

The Job endpoint negotiates MCP protocol `2026-07-28`. When the active Run has
a pending `human_input_requested` event, context and response tools return an
`io.modelcontextprotocol/input-required` result containing a bounded form
elicitation. An accepting client response is revalidated against the still-
pending request, recorded through the Run human-response API, and the original
tool request resumes. When a bounded response agent declares
`watch_operator_activity`, `watch_job_activity` asks the owner-node Job response
agent to resolve that Run-scoped MCP service and relay its SDK activity envelope
through a second MRTR. This is an
active bounded watch, not unsolicited server push, and its transport receipt
does not acknowledge a durable operator notice.

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

Blueprint folders and ZIP uploads use the SDK's canonical blueprint/v1 loader.
`MN_API_BLUEPRINT_UPLOAD_LIMIT_BYTES` caps ZIP files (default 32 GiB, the format
maximum); extraction applies the same limit to their uncompressed contents.
The ordinary `MN_API_REQUEST_SIZE_LIMIT_BYTES` limit continues to apply to other
requests. Uploads validate documents without importing blueprint Python code.

Uploaded Job definitions, bundle replacements, and catalog Job definitions pass through full
blueprint preparation before submission, including configuration overrides,
dependencies, topology lowering, and runtime staging.

Repeated configuration PATCH requests with the already saved resolved
configuration reuse the prepared definition and keep the Job revision unchanged.

Starting a saved Job through `POST /api/v1/jobs/{job_id}/runs` reuses its prepared
definition when overrides are absent or do not change its resolved configuration,
matching `mn job start`. It does not rediscover the catalog, rebuild worker
resources, or rerun placement against transient node status. Changed configuration
still passes the normal preparation and revision-checked update before start.
If another request saves the same resolved configuration during preparation,
the run uses that prepared Job and discards its redundant preparation. A
different concurrent configuration change remains a conflict.

## Streaming read-only Job answers

`ask_job` on response-enabled read-only Job MCP endpoints accepts `stream: bool`,
default false. Omitted/false returns the existing completed JSON tool result,
for agent collaboration. True selects SSE on the same authenticated endpoint
and relays actual visible answer text using MCP progress notifications; callers
should request progress with a progress token/callback. Each notification message
is JSON with schema_version `mn.mcp.job_answer_delta.v1`, request_id, sequence
(starting at 1), and delta. The final result remains `mn.mcp.job_answer.v1`.

The API reads bounded cursor updates over the existing Job response RPC, at most
once per 50ms with a 250ms idle read wait. It cancels the runtime stream when
delivery fails or its task is cancelled. An interrupted stream is not converted
to an unrelated fallback answer. The relay has a 90-second overall deadline.
Bounded action-agent `ask_job` retains its existing non-streaming turn contract.

The new SSE transport keeps MCP's loopback Host/Origin protection enabled
(`127.0.0.1`, `localhost`, IPv6 loopback, with explicit ports). Existing JSON
transports retain their settings. Non-loopback SSE exposure requires an explicit
trusted-host configuration in the transport before deployment; it must not be
enabled by disabling rebinding protection.
# Run result publication

Blueprint submissions with shared output copies start the background event relay, even without a post-launch hook. While a run is active, the run events API also reads published `run_result_available` events directly from the job's shared submission. This makes service results available before a local run store or completion relay exists. On completion the relay copies stable outputs and publishes their result events. Clients can show every declared result without depending on blueprint-specific configuration.

## Interaction API (coordinated upgrade)

Authenticated resources under `/api/v1`:

- `GET /interactions`: snapshot plus opaque replay cursor.
- `GET /interactions/{id}`: authoritative request and receipt.
- `GET /interactions/events/stream`: push-driven SSE; send the snapshot cursor in
  `Last-Event-ID`. A resync event requires another snapshot. Disconnect cancels
  the gRPC subscription, never the interaction.
- `POST /interactions/{id}/responses` and `/acknowledgements`: send an
  `Idempotency-Key` header and `expected_revision`; responses also carry `answer`.
  Conflicts return HTTP 409 with a structured error code.
- `POST /interaction-test-sessions`, `POST /interaction-test-sessions/{id}/examples`
  with a named `preset`, and `DELETE /interaction-test-sessions/{id}` create and
  dispose isolated examples. These invoke no real co-worker actions.

The matching Core and SDK are required (HTTP 426 for unsupported capability). MCP
review elicitation includes the durable interaction identity in metadata and
resumes that identity even if another request becomes pending. Full legacy
producer migration and coordinated draining remain required before cutover.

### API preparation performance

Changed-configuration Job launches reuse a single resolved catalog entry for
preparation and the output relay. Input validation, model/resource preparation,
and optimistic revision checks still run. Monitor and snapshot requests reuse
their canonical Run read when resolving output identity; no Run state is cached
across requests.

Run `mn_test --suite performance.api` from `mn-system-tests` for isolated HTTP
latency and duplicate-work regressions. It includes real local blueprint
preparation with an injected Core, unchanged configuration sync, prepared starts,
and streaming Chat first-content/final-answer timings.

All-Runs aggregation overlaps up to eight independent owner reads per request.
Runtime diagnostics overlaps the runtime, Docker, and gateway probes. Partial
failures and response ordering are preserved; transport deadlines still apply.
The API benchmark suite includes slow-dependency fixtures with a five-second
per-sample regression budget, separately from its ordinary 500 ms p95 budget.

## Actionable launch errors

Hardware and scheduling failures use shared SDK codes and explain the cause
without requiring debug mode. For example, a 48 GiB memory requirement on a
24 GiB node reports `MN_MEMORY_REQUIREMENT_UNMET`, the required and available
amounts, and a hint to select a larger node or reduce the requirement.
CLI JSON and API Problem Details include numeric `problem_code` (for example,
1001 for memory requirements or 3001 for scheduling), `category`, `retryable`, and bounded
structured placement `details.blockers`. See [SPEC.md](SPEC.md#shared-admission-error-contract)
for codes and retry semantics.
Measured admission blockers also identify a validated friendly PC name and the
available/required resource amounts. GPU memory shortages use `2001` and suggest
stopping other GPU workloads or unloading unused models before retrying. Updated
Core and SDK services are required for measured run-start errors; admission
requirements remain enforced.

## Job performance

Authenticated `GET /api/v1/jobs/{job_id}/analysis` returns the SDK job analysis:
`job_id`, `snapshot_at`, `scope: recorded_history`, `history_complete`, `runs`,
`running_time`, and `tokens`. Coverage accompanies nullable duration/token values.
Unknown jobs use the normal not-found problem response; analysis deadlines return
504. Disconnects cancel further collection. No model calls or runtime starts occur.

## Shared assistance evaluation

Authenticated `POST /api/v1/assistance/evaluations` accepts `blueprint_id`, optional
`job_id` and `execution_id`, local `setup` readiness/mode/revision, bounded
`resolved_keys`, and optional `requested_goal`. It rejects caller-supplied
operational context, configuration fields, mismatched blueprints, and changed
executions. The response is `mn.assistance.v1` with an opaque revision, bounded
safe context, and one typed opportunity or null. It delegates policy to the SDK;
it never executes actions or grants permission. Context-only co-workers can use
this endpoint without enabling knowledge-backed Job responses.

Job `ask_job` and streaming-start inputs advertise optional `assistance_task`
metadata with bounded `goal`, `state`, `execution_id`, and `next_question` fields.
The API validates this shape and the current execution before attaching it to
runtime context. It does not accept permissions, effects, or arbitrary actions;
accepted-task bounded-agent plans are restricted to read-only steps. The existing
response-service enablement requirement still applies to Job answers.

The API requires `mn-python-sdk-common>=1.3.58.dev0,<2.0`, which includes
matching SCM development builds for local installation and the final release.

Deploy the updated SDK common/job-response components before this API, then
release desktop clients that depend on the assistance endpoint. Missing endpoints
are explicit incompatibilities, not a reason to recreate the runtime policy.

## Failed-run retry resources

`POST /api/v1/runs/{run_id}/retry-plans` accepts
`{"configuration_overrides": {"catalog_review.walltime_seconds": 3600}}` and returns
eligibility, preserved/retried steps, declared adjustable fields, the expected
attempt and checkpoint revision. Planning does not dispatch work.

`POST /api/v1/runs/{run_id}/retries` requires an `Idempotency-Key` header and a body
containing `expected_attempt`, `checkpoint_revision` and explicit
`configuration_overrides`. It returns HTTP 202 with the accepted attempt identity.
Persist and reuse the exact selection, key and settings after a lost response.
Core revalidates the checkpoint, ownership, lifecycle conflicts and replay safety.
Retry uses the existing run and job IDs; it does not seed a fresh workflow.

Inspection/listing labels `record_source: runtime|history`. Stored history without
its Core control record remains inspectable and explains why retry is unavailable.
Runtime-unavailable retry planning returns HTTP 503 rather than pretending the run
is missing. Resume continues paused work; changed inputs require a new run.

## Job ZIP backup and restore

Authenticated `POST /api/v1/jobs/{job_id}/backups` returns a private
`application/zip` full offline capsule through the shared SDK. Pause active runs
first. Temporary download files are removed after delivery.

Authenticated `POST /api/v1/job-restorations?start=true` accepts the raw ZIP body
with `Content-Type: application/zip` and returns a `201` new job identity, start
status and optional run ID. Omit `start=true` to restore ready work. ZIP validation,
platform and destination hardware admission precede dependency preparation and job
creation; failures return HTTP 422 problem responses. This route permits at most
128 GiB and 100,000 capsule entries, while existing route limits remain unchanged.
Temporary uploads are removed on completion or failure. Restore does not invoke
blueprint additions, validation by catalog ID, or hiring. If start fails after
creation, the response retains the new job ID and an actionable `start_error`.

Requires matching Core `ExportJobBackup` / `RestoreJobBackup` streamed RPCs and
SDK `mn.backup.v3` support. The destination runtime, Python and Docker installation
must already be available on a compatible OS/architecture/Python ABI.


## Collaboration group catalog contract

Catalog projections preserve the SDK-validated `mn.collaboration.group.v1`
contract: `topology: group`, protocol, fixed goalId, mutually accepted blueprint
IDs, member capacity (2–16), goalKey/commonGoalKey, groupKey, peersKey, and
sameRuntime. Group peer configuration uses stable `{jobId, blueprintId}` entries.
Invalid declarations and private metadata are omitted. Legacy pair declarations
remain capacity two. This contract does not launch work or grant approvals.


## Preparation timing

Blueprint preparation and job submission write local INFO events
`worker.preparation.start` and `worker.preparation.finish`, including a static
stage, outcome and elapsed milliseconds. Timings cover bundle validation,
workflow/dependency resolution, packaged models, sandbox images, host Python
environments, payload/runtime staging, native resources and job submission.
They are recorded even without a launch-progress ID. Labels, details,
configuration, commands and exception contents are excluded. Progress and
logging sink failures do not change the submission result or deadlines.
