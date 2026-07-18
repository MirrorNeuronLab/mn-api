# AGENTS.md

Instructions for coding agents working in this repository. These instructions
apply only to `mn-api`.

## Start Here

1. Read `SPEC.md` for the repository contract and ownership boundary.
2. Read `README.md`, `pyproject.toml`, the affected route/helper, and its tests.
3. Check `git status` and preserve unrelated work.
4. Identify whether the requested behavior is HTTP adaptation or shared
   MirrorNeuron behavior. Shared behavior belongs in `mn-python-sdk`; this repo
   owns FastAPI validation, transport, streaming, and response shaping.

## Repository Map

- `mn_api/app.py`: application factory, middleware, exception handlers, routers.
- `mn_api/routes/`: versioned HTTP, SSE, and WebSocket endpoints.
- `mn_api/schemas.py`: API request and response models.
- `mn_api/dependencies.py`: authentication and request-size enforcement.
- `mn_api/errors.py`: problem responses and SDK/gRPC error translation.
- `mn_api/config*.py`: supported configuration, precedence, and validation.
- `mn_api/blueprints.py`, `artifacts.py`, `run_outputs.py`, `run_store.py`:
  transport-adjacent helpers for larger route families.
- `mn_api/state.py`: process-level configured clients and services. Tests replace
  these dependencies; do not make import-time network calls.
- `tests/`: route, contract, configuration, error, and helper coverage.

## Contract Rules

- Keep public routes under `/api/v1` unless a new interface version is
  intentionally introduced.
- Preserve the top-level interface `version` field and established endpoint
  aliases. Contract changes require tests and documentation.
- Return structured, sanitized errors. Use the helpers in `mn_api.errors`; do
  not expose tracebacks, credentials, raw subprocess output, or internal paths.
- Enforce bearer authentication consistently for HTTP and WebSocket routes when
  `MN_API_TOKEN` is configured.
- Treat request bodies, query values, artifact paths, manifest data, and
  upstream SDK/gRPC responses as untrusted.
- Keep route functions thin. Put reusable domain-neutral behavior in the SDK
  and API-only composition in focused helpers.
- Keep long-running launch, stream, and cleanup flows bounded and cancellation
  aware. Do not block the event loop with unbounded synchronous work.
- Preserve dependency injection used by tests. Normal unit tests must not need
  Redis, Core, Docker, OpenShell, a live catalog, or the network.

## Change Workflow

- Update the closest request/response model and route test with every behavior
  change.
- For error changes, cover HTTP status, media type, error code, sanitization,
  and any compatibility alias.
- For WebSocket/SSE changes, cover authentication, disconnect/termination, and
  stable event shapes.
- For config changes, update `config_schema.py`, `.env.example`, `README.md`,
  tests, and `SPEC.md` when the public contract changes.
- Do not hand-edit generated package metadata under `mirrorneuron_api.egg-info`.

## Verification

Use the narrowest test first, then the repository gate:

```bash
python -m pytest tests/test_<area>.py -q
python -m ruff check .
python -m pytest
python -m build
```

The configured full test run enforces branch coverage of at least 85 percent.
If a live dependency was intentionally required and unavailable, report that
separately; do not weaken deterministic tests to compensate.

## Issue-Fixing Policy

- Fix the root cause in the owning layer unless the user explicitly requests a
  temporary workaround.
- Do not add fallback paths, compatibility shims, or feature flags that hide a
  broken primary path.
- Keep product-specified compatibility behavior narrow, documented, and tested.
