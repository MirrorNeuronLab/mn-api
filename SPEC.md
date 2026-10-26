# MirrorNeuron API Specification

## Invocation configuration during hiring

Durable Job preparation forwards nested input validation, dependency, image, and
worker preparation activity into its operation progress. Heartbeats retain the
same operation and reserved Job; observing progress never resubmits work.

`POST /api/v1/blueprints/{id}/additions` accepts optional `config_overrides`,
using the same SDK configuration resolution as launch. This lets explicit fake
model/skill sessions prepare prerequisites consistently while retaining bundle
validation. Overrides apply to that preparation invocation and are not persisted
as catalog defaults or operator settings. Request idempotency includes the
overrides; progress and addition records do not expose the configuration.

## OpenShell preparation

SDK 1.3.61.dev41 or matching source is required. Job creation and executable
configuration updates prepare canonical
`MirrorNeuron.Runner.OpenShell` workers through the SDK after placement and
payload staging. The selected native owner builds declared custom images with
runtime dependencies, preserves rendered policy restrictions, and creates the
definition-scoped shared sandbox. The API stores the definition only after a
matching receipt supplies its sandbox identity. Native owners must advertise
`openshell_prepare_v1`; missing capability or preparation failure is an operation
failure, never a successful unprepared Job. The API does not maintain a separate
image-builder implementation or import CLI provisioning code.

OpenShell preparation failures preserve the SDK's three `MN_OPENSHELL_*` codes
for image build, gateway connection and sandbox creation, plus allowlisted
`details.stage` and optional `details.node`. Synchronous Problem Details and
durable operation polling/SSE expose the same non-retryable HTTP 412 failure.
Raw subprocess output and arbitrary diagnostic fields remain private. Deploy
with the matching SDK's OpenShell error contract.

The API requires Python 3.12 or newer. The managed runtime and contributor/CI toolchains use Python 3.12, matching the SDK and CLI.

## Durable preparation and reply submission

`POST /api/v1/job-preparations` accepts `JobCreate` with an `Idempotency-Key`, reserves a stable Job identity before dispatch, and returns `202` with a durable operation and `Location`. `POST /api/v1/jobs/{id}/preparations` accepts configuration-only `JobUpdate` with `Idempotency-Key` and `If-Match`. Follow `/api/v1/operations/{id}` or its authenticated SSE stream. Reading a failed preparation never restarts it; explicitly reposting retries under its original reserved Job and conditional fence. Reposting the same claim reconnects to it; after API restart the original Job identity and conditional revision fence are reused. A changed active/completed creation payload returns `409` with the original operation location for reconciliation. A corrected failed preparation receives a new operation for the original reserved Job. Synchronous REST resources remain available for existing direct clients; OtterDesk uses only the asynchronous preparation path.

Job MCP `ask_job` advertises `background` and `reply_action` (`start`, `read`, `cancel`) for both read-only and bounded-agent replies. Async calls require a stable request ID and preserve the question, original conversation ID and assistance intent across reads. A read-only activity-control capability probe verifies owner support before dispatch. Each call returns `mn.mcp.job_reply_operation.v1` with Job/blueprint/request/operation/conversation identity, status, cumulative bounded text, and a final `response` retaining the existing answer contract. The Job owner persists its claim before running the answer. Relay unavailability returns `mn.mcp.job_reply_observation.v1`; clients reconnect to the original claim instead of treating it as an owner failure. Connections observe work; they do not own its lifetime. Read-only cancellation is explicit. Effectful interruption is reported for reconciliation and never automatically replayed. Deploy the matching SDK job-response component on each owner and restart the native SDK service alongside the API.


## Blueprint review metadata

Blueprint list and detail resources preserve the SDK catalog's `skills` list
as `{name, version_constraint}` entries and the explicit `air-gapped` boolean.
Skill source locations and other dependency metadata are excluded by the shared
projection. An empty list denotes no declared skill dependencies. These read-only
resources report requirements, not installed versions or per-run invocation
history, and do not prepare runtime resources.

## Purpose

`mn-api` is the HTTP gateway for the local MirrorNeuron runtime. It translates
HTTP and Server-Sent Event interactions into calls to the
MirrorNeuron Python SDK and returns browser- and desktop-consumable responses.
Blueprint run requests apply the SDK-owned input validator before hardware,
model, helper-process, or submission work and return a field-level HTTP 422
problem when required configuration is missing.
The package installs the `mn-api` service and the `mn-web-ui-server` static/proxy
service.

This specification covers only this repository. Core runtime semantics and
shared Python behavior are external contracts consumed through
`mirrorneuron-python-sdk`.

Run detail and monitor responses expose
Core's `placement` with `owner_node`, friendly `node_label`, and a current
`reservation` summary. Its `state` is `reserved`, `queued`, `unreserved`, or
`unavailable`; held claims include allocation mode, CPU cores, RAM/disk MB,
GPU count and GPU memory MB. Unknown quantities remain null. This is admission
accounting, independent of measured usage. Monitor reads reuse the same canonical
Run, overriding any replica placement. Offline history returns null placement.
The matching Core detailed Run projection is required for these fields.

Compact run monitors bind resource aggregation to the same resolved run
directory as their artifacts. Core's explicit storage reference takes precedence
over local mappings; an unavailable explicit reference does not fall back to a
different directory. Missing or mismatched directory identity leaves
resource usage unavailable; no second local-root lookup substitutes empty or
unrelated measurements. The SDK owns numeric usage aggregation and its bounds.
The dependency minimum is SDK 1.3.58.dev83, including canonical measured call
ledgers, stable Job conversation sources and bound container output paths.

Run read endpoints may project a run from a verified SDK local mapping and its
replicated submission when Core cannot return the run. A nonterminal shared
record is reported as `unknown`; writable Core run operations still require Core.
Human review annotations are separate from Core run control: an authenticated
response to a pending request writes to the verified mapped submission ledger
when that ledger exists, and otherwise to the local run ledger. Unknown or
closed requests return HTTP 409.
API-started durable Job runs with a declared host output copy start the SDK host
delivery relay, including starts that reuse an unchanged definition. The API
loads the committed submission's copy contract without rebuilding the catalog
bundle. It also reconciles completed Core-scheduled runs from replicated
completion receipts. The relay reports delivery separately from Core execution
completion.

Canonical final-result, artifact-list/download, and output-list/download GETs
read from the run's Core-selected owner when that node advertises an enabled
native HTTP API. This avoids coupling completed-file availability to the
ingress computer's asynchronous shared-storage replica. Each request reads the
canonical Run once; Core membership selects the peer's advertised host/port.
The receiving API must confirm the same execution, owner, data generation and
storage reference before serving bytes. Changed bindings or a second hop fail
with HTTP 409. Authentication precedes routing; only configured peer API
credentials are sent. Redirects and environment proxies are disabled. Reads
use 15-second I/O timeouts and 64-KiB streaming chunks, with upstream closure on
completion, failure and cancellation. Byte ranges and download metadata survive
the relay. No caller-provided target URL or host path is accepted.
Local owners, legacy owners without an HTTP advertisement, and deliberately
disabled HTTP endpoints retain the existing exact-binding replica reader.
Failures from an advertised enabled owner never substitute a stale local copy.
Both APIs need this run-file protocol: the peer acknowledges the binding through
`X-MN-Run-File-Binding`, under a single `X-MN-Run-File-Hop`. Direct authenticated
clients keep the same public routes, JSON shapes and downloads. The documented
offline replica read remains available when Core cannot return a Run; it cannot
acknowledge an owner routing request without verifying its binding.

Blueprint additions and launch validate declared runtime model policies and
hardware compatibility without installing generation models. Launch retains
`model_install` as the compatibility phase identifier for this check. The SDK
selects, prepares, and routes models on first actual use, reusing installed
artifacts and preserving both the automatic `default` policy and explicit
model requirements. Required context services retain their existing
preparation lifecycle. Explicit model-install routes remain eager.

The SDK default policy is medium → small → nano: Nemotron 3.5 Lightning at
48 GB accelerator memory, Bonsai2 27B PTQ1_0 at 24 GB, then Gemma 4 E2B.
VRAM and integrated/shared GPU memory qualify; CPU RAM alone does not.
Bonsai2's catalog `preparation` recipe makes the SDK automatically build/cache
and start the pinned native Prism DMR backend on the selected model owner.
The API adds no separate backend installation or endpoint-routing mechanism.

`GET /api/v1/models` defaults to the full merged catalog plus discovered
installations; `installed_only=true` preserves the installed-only filter.
Model records expose `default: true` for the configured default and its fallback
chain. `PUT /api/v1/models/default/installation` prepares the first compatible
configured model on a local or cluster node without changing default policy.
Model installation runs as an API-owned background operation through SDK
preparation and gateway synchronization. The existing `202`, operation
Location, authentication, request fields, and idempotency contract apply to
both logical defaults and explicit model IDs.

Managed catalog records expose `source: "dmr"` or `source: "docker"`.
Explicit Docker IDs use the same installation operation, with structured
Docker settings and credential environment references resolved by the SDK on
the selected native owner. NIM readiness precedes successful registration and
gateway synchronization. The API does not accept registry credentials in
installation bodies or implement a separate Docker lifecycle.

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

Job creation and blueprint-run requests accept the optional observation header
`X-Launch-Progress-ID`. Authenticated `GET /api/v1/launch-progress/{progress_id}`
exposes presentation-only snapshots while the existing synchronous request is
pending. The view excludes configuration, validation payloads, and raw failure
diagnostics; it retains at most 200 recent events. Blocking preparation stages
refresh their current description and elapsed time every ten seconds. Reporting
threads end with the wrapped call and never execute, retry, or cancel a job.
No artificial completion percentage is inferred from elapsed time.

- Every REST path is rooted at `/api/v1`. `/api/v2`, historical aliases,
  runtime-run paths, and WebSocket routes are not mounted.
- `GET /api/v1/health` advertises `api_contract: "mirrorneuron.rest.v1"`.
- REST requests and responses do not contain a transport `version` field.
- The surface includes runtime/system health, blueprints, bundles, jobs, runs,
  schedules/events, deployments, models, services, resources, artifacts, and
  realtime progress.
- `DELETE /api/v1/nodes/{node_id}` removes a federated peer through Core and
  returns its node name and removal status.
- Existing model-remote and model-proxy REST paths are compatibility adapters
  over the SDK-owned `$MN_HOME/models/registry.json`; they do not restore the
  removed CLI proxy/manual-remote lifecycle or project legacy ledgers.
- Collection responses contain `items` and an opaque `next_page_token`.
- Streaming endpoints must terminate on completion, error, timeout, or client
  disconnect and must not leak background tasks.
- Job workflow definition and latest-run shape endpoints expose public step
  dependencies and step-agent membership. Workflow-progress polling and run
  event-stream snapshots expose status and progress without shape fields. Hidden
  lowered runtime nodes such as start/end/fork/join nodes are transitively
  projected into source-facing edges and layers at the API boundary.
- Run workflow-progress uses the saved manifest for that execution ahead of a
  mutable stable Job definition. It uses the durable workflow ledger for step
  state when available and replays up to 5,000 recent events for activity; runs
  without a durable ledger replay the full event history. A root failure is
  present only while the run is failed; earlier retry errors remain in events.
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
  `POST /jobs/{job_id}/run-operations` accepts an `Idempotency-Key` and returns
  a durable API launch operation immediately. Its authenticated operation SSE
  stream reports progress and terminal success with the created `run_id`, or a
  sanitized failure. Replaying the same request after an API restart resumes an
  interrupted operation and reconciles a Core update that may have committed
  before its RPC deadline. A changed body with the same key is rejected.
  Temporary contention on the selected PC accepts an automatically queued run
  with HTTP 202 and `status: queued`. An unreachable owner or unavailable
  scheduling authority can still return `MN_SCHEDULING_UNAVAILABLE`; permanently
  incompatible hardware is rejected. Other operations keep their existing errors.
  A blueprint-owned Web UI is optional singular Job state: its durable handle
  is served only at `GET /api/v1/jobs/{job_id}/ui` and is shared by every run
  of that Job. Run UI paths are not mounted. The local `mn-web-ui-server` may
  use that authenticated handle to proxy a declared external service UI and
  its explicitly allowlisted companion ports only while the handle is running;
  paused, stopped, cancelled, and failed handles are not proxied. The API
  itself does not expose a general remote-proxy route.
  Job UI registry discovery queries only the Job's owner node under its latest
  execution ID; unrelated federation peers are not queried. Without an owner,
  discovery skips the registry and uses the existing durable-handle path.
  Job UI discovery rechecks the exact page and its same-origin code assets
  through the shared Web UI SDK probe from the proxy host. Registration or a
  previously passing service check cannot establish readiness. The returned
  page and assets are checked through the existing iframe proxy at the configured
  runtime Web UI URL. Only authenticated probe requests read the address/lifecycle
  descriptor with `check_readiness=false`, avoiding recursive discovery while
  retaining lifecycle and port restrictions. Descriptor reads do not establish
  readiness; ordinary discovery defaults to a fresh check. Probe authentication
  never reaches worker services. Ordinary iframe and WebSocket requests continue
  to reject unready handles. The returned
  `web_ui.metadata.readiness` receipt is current; an unreachable running handle
  is projected as `starting` without rewriting durable lifecycle state. Inactive
  handles are never promoted. The proxy rejects unready handles and forwards
  live multipart bytes as they arrive rather than waiting for a full buffer.
  Discovery carries `metadata.load_event` (`dom-ready` by default, optionally
  `did-finish-load`) from the current service or its matching SDK claim. This
  desktop visibility policy never bypasses the page/asset readiness check.
  Only executable `type: service` jobs are single-run: ordinary second starts
  return HTTP 409 Problem Details with code `service_run_exists`, while explicit
  `replace_existing_run` requires a fresh caller-supplied `run_id` and returns
  that run plus optional replaced-run and deferred-cleanup metadata.
  Retry/recovery attempts retain their run ID.
- Schedule creation accepts `Idempotency-Key` and replays identical requests.
  Schedule detail read and update are pending Core/SDK RPC support.
- Archive retains shared data. Data reset and permanent job deletion are
  explicit operations; confirmed deletion is rejected while runs are active.
  Individual run deletion never deletes job data.
- Stable `job_id`, execution `run_id`/`execution_id`, and internal diagnostic
  `runtime_run_id` are separate identities. Clients use only `run_id` in URLs.
  Output and saved-event reads use the canonical `run_data_ref` while work is
  active, ahead of a final result reference. File access resolves and verifies
  the exact SDK submission/run binding without requiring a legacy local mapping.
  Run SSE uses that same output
  identity for saved events and keeps the execution ID for progress/control.
  Job MCP context selects the Job's explicit latest-run identity rather than
  the first collection entry. Its workflow evidence uses the execution ID and
  its file reads verify the canonical submission/output binding. Bounded MCP
  actions and service activity watches resolve services under the execution ID,
  independently of the output ID used for artifacts and saved notices.
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
- Local-only blueprints with a host OS requirement prepare HostLocal Python on
  the native host and prepare a separate Core SDK proxy. Native preparation
  must return `mn.native.host-python.v1` and the proxy environment before
  submission. Extras and local SCM version identities use shared SDK helpers;
  API installation includes the SDK's `local-source` extra.
  The SDK dependency floor is `1.3.58.dev46`, below `2.0`.
- Background output relays poll the execution run ID, which remains separate
  from the durable job ID used for definition paths and launch responses.
  Starting a catalog-backed stable Job through `/jobs/{job_id}/runs` creates the
  same per-run mapping and relay as `/blueprints/{blueprint_id}/runs`, including
  when no configuration override is supplied.
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
  Creation and executable configuration updates run host-side command input
  validators before storing the definition. The SDK records those successful
  checks and removes command rules that Core is intentionally forbidden to run.
- Every prepared `mn.mcp` Job exposes the standard supervision profile and
  declared domain tools at its stable Streamable HTTP endpoint, independently
  of conversation model availability. Shared results use the standard envelope
  described below; bounded context retains `mn.mcp.job_context.v1` inside data.
- `ask_job` accepts an 8,000-character question, an optional UUID conversation
  ID, and an optional 128-character request ID. It returns the bounded
  `mn.mcp.job_answer.v1` contract through Core's owner-routed unary query,
  never creates a Run, and has no REST, SSE, or UI chat equivalent.
- `watch_job_activity` performs a bounded active wait. When the blueprint's
  MCP extension declares an activity binding, the API routes the
  watch to that Job's owner node. The Job response agent resolves exactly one
  passing Run-scoped service and uses the SDK MCP client to receive its activity
  through MRTR before the API relays it to the chat client through MRTR.
  Delivery receipt is transport-only and never acknowledges a review notice.
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
- `mn_api.config`, `mn_api.config_env`, and `mn_api.config_schema` remain
  compatibility facades over SDK configuration loading and typed shared keys.
  API-only keys are composed locally. Precedence is `environment >
  .env.<profile> > .env > defaults`, including explicit blank environment
  overrides.
- `MN_MODEL_CATALOG_PATH` selects SDK-owned model defaults, entries, and
  fallback links. The API carries no physical built-in model policy.
- Sensitive configuration values are redacted based on schema metadata and
  secret-like key names.

The current supported keys and defaults live in `mn_api/config_schema.py` and
are documented in `.env.example` and `README.md`.

## Dependency Boundary

Routes call shared clients and helpers rather than duplicating SDK business
logic. External services are supplied through configured state/dependencies so
tests can use fakes. Importing the package or constructing the app must not
require live Core, Redis, Docker, OpenShell, or network access.
Public workflow manifests and bounded job activity are projected by SDK
helpers; routes only add HTTP transport concerns.

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


## Canonical blueprint packages

All blueprint folders and ZIPs use the blueprint/v1 manifest schema and role
documents owned by `mn_sdk.blueprints`. Catalog indexes contain ordered package
paths only. Catalog reads are data-only; explicit compilation produces Core's
runtime manifest. Default configuration, local overwrites, and invocation
values resolve through the SDK. Payload assembly stages one resolved descriptor;
launch environment is passed explicitly. Confirmed failure permits owned
resource rollback; uncertain submission acknowledgements require reconciliation.
See `mn-docs/blueprint-standard.md` for document ownership and extension schemas.

Blueprint uploads have a separate configurable file-size limit, capped by the
SDK format-v1 maximum of 32 GiB. Multipart framing has a 1 MiB allowance. Other
requests retain the ordinary HTTP body-size policy. Upload files and extracted
contents are independently checked before a package can be launched.

A Job configuration PATCH that repeats the saved resolved configuration reuses
the prepared definition and keeps the Job revision unchanged. Other requested
attributes still update normally.

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

## Shared admission error contract

CLI and API use `mn_sdk.error_catalog.ERROR_CATALOG` for admission error codes,
category, safe message, remediation hint, HTTP status and retryability. Existing
codes remain stable. Unknown errors retain `MN_EXECUTION_FAILED`; raw exception
text never becomes an admission explanation. Applications must branch on codes,
not message text. `AppError.category` and `AppError.retryable` are additive.

| Problem code | Symbolic code | Category | HTTP | Retryable |
| --- | --- | --- | --- | --- |
| 1001 | `MN_MEMORY_REQUIREMENT_UNMET` | hardware | 422 | false |
| 1002 | `MN_CPU_REQUIREMENT_UNMET` | hardware | 422 | false |
| 1003 | `MN_GPU_REQUIREMENT_UNMET` | hardware | 422 | false |
| 2001 | `MN_GPU_MEMORY_UNAVAILABLE` | capacity | 503 | true |
| 2002 | `MN_DISK_UNAVAILABLE` | capacity | 503 | true |
| 2003 | `MN_RESOURCE_EXHAUSTED` | capacity | 503 | true |
| 3001 | `MN_SCHEDULING_UNAVAILABLE` | scheduling | 503 | true |
| 4001 | `MN_PLACEMENT_UNSATISFIED` | placement | 422 | false |

Hardware errors require a configuration or hardware change. Capacity and
scheduling errors may succeed after availability changes; retryable is not a
promise of success or authorization to replay a submission automatically.
A mixed placement failure reports all observed blockers rather than claiming
that every node lacks memory. Unknown/mixed causes require inspection.

Placement errors include bounded `details.blockers` with code, safe message,
one-based node index, and (when measured) required/available amounts, `unit`,
`resource`, and `operator` (`>=` or `>`). A validated `node_label` may identify
an explicitly reported friendly PC name; raw runtime node IDs and addresses are
not labels. Node indices refer to the sorted placement snapshot, not persistent
node IDs. Preparation host-memory requirements use total capacity. Core run
admission reports memory available for runs after limits and reservations.
GPU device memory uses free capacity, preserving a reported zero. Values are
shown in GiB (1024 MiB), matching existing placement conversion. Missing
measurements are not invented. Public summaries contain no paths, secrets,
raw messages, or arbitrary diagnostics; they show up to eight blockers, while
structured details contain up to 100. Truncated reports retain the general
placement code rather than claiming a cluster-wide resource shortage.

Core appends a bounded `mn_admission_v1` JSON marker to `placement_failed:` gRPC
details. The SDK validates the versioned blocker fields, owns the numeric error
catalog and messages, and emits the same `AppError` through CLI and REST. For
example: `spark has 8.17 GiB of free GPU memory; this work requires 48 GiB.`
Code `MN_GPU_MEMORY_UNAVAILABLE` / `2001` suggests stopping other GPU workloads
or unloading unused models before retrying. Busy CPU/GPU reservations use
`MN_RESOURCE_EXHAUSTED` / `2003`, while insufficient CPU/GPU hardware retains
`1002` / `1003`. Requirements and scheduling decisions remain unchanged.
Malformed, unknown, or legacy generic device errors do not imply a memory cause.
Legacy `RuntimeError` catches still work for preparation placement failures;
normalization retains structured identity through launch exception wrappers.

Core's legacy overload and no-schedulable-node markers remain normalized at
run-start boundaries. Other operations retain their existing transport error
interpretation. No protobuf shape or numeric code changes are required. Deploy
the updated Core and SDK together to enable measured run-admission errors;
older Core versions retain their existing, less specific error behavior.

### Numeric problem codes for automation

`problem_code` is a stable integer shared across SDK errors, CLI JSON, API
Problem Details and individual placement blockers. It is independent of HTTP
status and process exit code. Human CLI output also prints the numeric code.
Existing symbolic `code` remains backward compatible. SDK consumers use
`ProblemCode` (IntEnum), `PROBLEM_CODES` (numeric-to-symbolic lookup), and
`problem_code(symbol)`; `ERROR_CATALOG` holds admission defaults.
Assigned values must never be renumbered or reused. Message wording can evolve
without changing a problem code. Clients must handle unrecognized values as
unknown errors and must not automatically retry them. An unregistered SDK
symbol maps to 9000; an unexpected execution failure maps to 9001.

Ranges reserve related problem families: 1xxx hardware, 2xxx capacity,
3xxx scheduling, 4xxx placement, 5xxx input/configuration,
6xxx access/resource state, 7xxx runtime/transport, 8xxx cancellation,
9xxx unknown/internal failures. Exact codes, rather than ranges or message
matching, drive automated remediation. `category` provides a finer label.

Example API problem (HTTP 422):

```json
{
  "problem_code": 1001,
  "code": "MN_MEMORY_REQUIREMENT_UNMET",
  "category": "hardware",
  "status": 422,
  "detail": "Host memory: requires 48 GiB; available 24 GiB.",
  "hint": "Select a node with enough memory or reduce the workflow's memory requirement.",
  "retryable": false
}
```

```python
from mn_sdk import ProblemCode

if response["problem_code"] == ProblemCode.MEMORY_REQUIREMENT_UNMET:
    # Choose a larger runtime or change requirements before submitting again.
    pass
```

## Job performance

Authenticated `GET /api/v1/jobs/{job_id}/analysis` returns the SDK job analysis:
`job_id`, `snapshot_at`, `scope: recorded_history`, `history_complete`, `runs`,
`running_time`, and `tokens`. Coverage accompanies nullable duration/token values.
Unknown jobs use the normal not-found problem response; analysis deadlines return
504. Disconnects cancel further collection. No model calls or runtime starts occur.

## Shared assistance adapter

The common dependency floor is `1.3.58.dev0`, below `2.0`, so both SCM
development builds and final releases providing this contract can be installed.

`POST /api/v1/assistance/evaluations` is authenticated and read-only. Strict
request models accept a selected blueprint, optional stable Job/execution,
non-secret local setup readiness/mode/revision, dispositions, and an explicit
requested help kind. The API verifies identities against its runtime context,
projects pending input and declared batch capabilities, and delegates evaluation
to SDK common. Strict response models expose `mn.assistance.v1`, a revision,
bounded context, and a typed supported action opportunity. Context-only access
does not relax the separate response-service gate for MCP/Job answers. Existing
setup, scheduler, lifecycle, and interaction APIs remain authoritative for effects.
Optional `assistance_task` on Job answers and streaming starts has only bounded
goal/state/execution/next-question metadata. It is checked against the current
execution; it cannot grant permissions or expand the bounded-agent effects.

## Durable checkpoint retry

Authenticated v1 run resources expose POST `retry-plans` and `retries`.
Planning accepts only bounded explicit integer/string setting overrides.
Submission requires a positive expected attempt, SHA-256 checkpoint revision and
an `Idempotency-Key` header. Unknown fields and malformed selections are rejected
before Core invocation. Core is the sole eligibility/idempotency authority.
Submission returns 202 and the accepted attempt identity under the same run ID.
Stored history remains inspectable with its record source and missing/unavailable
control status; historical presence must not mask Core unavailability for retry.

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

## Standard runtime activity

Runtime events use the shared [`mn.runtime.event.v1` contract](../mn-python-sdk/docs/runtime-activity.md),
including level, blueprint owner, UTC timestamp and actor attribution.
The SDK selects meaningful activity for the CLI, API, desktop and web monitors;
raw events remain available for diagnostics.


## Standard Job MCP supervision

Every blueprint declares the required `mn.mcp` extension independently of
`mn.response`. The versioned profile is `mn.mcp.supervision.v1`; the public
endpoint is `/api/v1/jobs/{job_id}/mcp`. The prepared Job stores the installed
manual from `knowledge/user_manual.md`, its SHA-256, and its declared tool
schemas. Discovery works before a run and while a domain service is offline.

The SDK MCP component owns these common tools: `get_user_manual`,
`get_capabilities`, `get_job_profile`, `get_job_context`, `get_run_status`,
`get_run_results`, `start_run`, `pause_run`, `resume_run`, `stop_run`,
`watch_job_activity`, and `get_operation_status`. `ask_job` and `get_job_turn`
are optional conversation tools. Asking never starts or resumes work. Reads
without a run ID select the latest run and return an explicit empty run when
work has never started. Pause preserves resumability; stop cancels execution.
Run controls accept only runtime-issued execution IDs belonging to the bound
Job. Output IDs remain distinct from Job and execution IDs.

Domain tools use unprefixed verb-resource `lower_snake_case` names, descriptions,
native JSON argument types, input/output schemas, and effect annotations.
Shared names are reserved. Readiness, service selection, activity, and async
completion bindings belong in the declaration. The SDK stages the manual and
registers, validates, and wraps declared tools; blueprints implement domain
behavior. Domain polling uses the same `get_operation_status` public surface.

Every ordinary result uses `mn.mcp.tool_result.v1` in `structuredContent`, with
`tool`, `request_id`, `job_id`, `blueprint_id`, `run_id`, `status`,
`operation_id`, typed `data`, and structured `error`. Operation states are
`accepted`, `running`, `completed`, `failed`, `cancelled`, `superseded`, and
`timed_out`; actual workflow state stays in `data`. Acceptance is not observed
completion. Mutations require caller-generated request IDs; durable Job-scoped
claims deduplicate identical retries and reject conflicting reuse. Operation
polling stays bound to its original Job and run. MCP input-required responses
continue to use the existing MRTR protocol.

Launch credentials travel only in authenticated, main-process-supplied
`_meta["mn.launch"]` request metadata, validated through the existing launch
contract. They are excluded from advertised arguments, tool results, logs,
conversation history, and durable operation records. REST and MCP transitions
use the same SDK integration helper; Core owns actual execution state. The API
owns authentication, endpoint identity, and owner routing.

This is a coordinated replacement with no public compatibility aliases.
`get_latest_run` is replaced by `get_run_status`; `free_play` is renamed
`start_free_play`. Configuration, scheduling, approvals, retries, and destructive
data management retain their existing APIs. GTM peer collaboration retains its
separate read-only capability and gains no lifecycle authority.

`PATCH /api/v1/jobs/{job_id}` accepts `refresh_contract: true` with `If-Match` to reprepare the installed catalog definition even when configuration is unchanged. It preserves stable identity and data, and rejects active-run replacement with 409.

## External MCP connections

External access is off for each Job until its owner enables it. The owner API
(`MN_API_TOKEN`) must be configured before enabling external access. Authenticated
`GET /api/v1/jobs/{job_id}/mcp-access` reads policy; `PUT` accepts
`{ "enabled": true, "server_url": "https://runtime.example/api/v1/jobs/<job-id>/mcp" }`.
Only HTTPS or local loopback HTTP URLs with this exact Job path are accepted.
Enabling or reconnecting returns one connection code to the owner and revokes
previous external grants. Disabling revokes external access and refresh tokens.
Internal owner requests and authenticated, Job-scoped `/mcp-sessions` grants
continue working independently of this opt-in. Existing GTM collaboration remains
a separate read-only capability.

Clients use Streamable HTTP at the displayed Job MCP URL. OAuth protected-resource
discovery is at `/.well-known/oauth-protected-resource/api/v1/jobs/{job_id}/mcp`;
path-aware authorization-server discovery is at
`/.well-known/oauth-authorization-server/api/v1/mcp/oauth`. The authorization server
uses public-client dynamic registration, authorization code with S256 PKCE,
Job-bound audience and scopes, atomic one-use codes, rotating refresh tokens,
and revocation. The browser consent form requires the separately copied
connection code and describes lifecycle authority. Its form policy permits only same-origin submission
and the registered callback origin so Chromium can follow the OAuth redirect;
scripts and framing remain prohibited. Owner credentials are never
shared with external clients. Connection codes and tokens are stored as hashes
in a private SQLite journal; OAuth grant metadata is bounded by expiry.

The SDK serves MCP Skills discovery and manifest resources on the same endpoint,
including the prepared manual and installed tool schemas. External clients cannot
supply native `mn.launch` metadata; only owner/internal desktop sessions may pass
ephemeral launch credentials. Their values never enter tool arguments, results,
operation journals, or conversation history.

Remote clients such as ChatGPT need a reachable HTTPS runtime address or a secure
MCP tunnel. Preserve the external host and forwarded HTTPS scheme at your proxy.
The runtime and desktop do not publish a tunnel automatically. Copying a setup
prompt provides instructions; the user still adds and authorizes the connection
in their AI client's MCP settings.

Local-source MCP integration requires the matching SDK/common/MCP family from `1.3.61.dev12` onward, below `2.0`. This floor accepts the source revision containing supervision and Skills; it does not require an unpublished `1.3.62` family.


## Automatically queued starts and schedule controls

`POST /jobs/{job_id}/runs` returns HTTP 202 for an accepted start, including when
its selected PC is busy. A queued result has `status: queued` and an `admission`
object with durable `queue_id`, `owner_node`, node label, position and blocker.
Launch-operation and MCP supervision results preserve that receipt. Run detail
and monitor projections include the current admission state. Queue acceptance is
acknowledged immediately and requires no extra confirmation.

Each PC runs one MN run at a time; that run reserves CPU, memory and all GPUs,
shared by its workers and models. Different PCs run independently. Ownership is
checked at the authoritative owner Core, including remote starts forwarded using
federation credentials. Cached inventory is informational. Permanently
incompatible requirements still fail validation; unreachable owners report
unavailability and do not grant a claim. Requests use their original idempotency
identity when reconciled after an uncertain response.

`GET /schedules` inspects the connected Core (or `owner_node` query target).
`GET`, `PATCH`, `DELETE /schedules/{schedule_id}` use the same authenticated
owner-routed scheduling contract. PATCH/DELETE require the current strong
`If-Match`. For queue schedules (`queue_<run_id>`), PATCH
`{"schedule":{"run_at":"2099-10-20T12:00:00Z"}}` defers the same execution;
DELETE cancels only while unstarted. `PATCH /runs/{run_id}` with
`{"desired_state":"cancelled","queued_only":true}` additionally fences the
queued-only condition in Core, including admission races. A stale request never
stops the active run. Paused runs and unfinished cleanup retain the reservation;
no ETA is inferred. Core/native SDK capability `exclusive_run_v1` is required.

### Owner-scoped Job UI relay

The native SDK advertises the node's HTTP API as `hardware.http_api` with
schema `mn.http_api.endpoint.v1`, an enabled flag, scheme and port. Discovery and
relay use that port with the Core-registered node address; the caller's API port
never selects a peer endpoint. Missing, disabled or invalid endpoint metadata
fails with HTTP 503. Current owner handles take precedence over asynchronous
shared replicas so stale starting-state metadata cannot hide a serving UI.

The narrow `/api/v1/jobs/{job_id}/ui-proxy/{port}/{path}` GET/HEAD resource and
its `/ws/{path}` WebSocket companion relay only the current execution's declared
UI ports through its Core-registered owner. They do not accept target URLs.
Remote UI descriptors carry Core-derived `mn.web_ui.transport.v1`; the browser
keeps its local `/job-ui-proxy` URL while private Docker DNS is resolved only on
the hosting computer. Relays require the normal server-side API authentication,
`X-MN-UI-Execution` and one `X-MN-UI-Hop`. Changed ownership, stale registration,
inactive lifecycle and extra hops fail closed. Peer API authentication uses the
existing configured `MN_API_TOKEN`; differing credentials report failure. Tokens,
cookies and relay authority headers are stripped before the worker. Redirects
are not followed. Streams and WebSockets close their upstream on disconnect.
The API relay delegates to its own local Web UI proxy with an authenticated probe
marker, avoiding recursive readiness checks. Ordinary UI discovery still probes
the actual document/assets through the complete route.

## Admission and relocation resources

Run admission checks are read-only projections of owner Core policy. Manual run
requests default to start_now and must explicitly request enqueue_if_busy. Job
relocation is an authenticated durable operation with a stable transfer identity;
HTTP adaptation must not implement ownership or filesystem-copy policy.
