# MirrorNeuron API

`mn-api` is the FastAPI REST gateway for MirrorNeuron. It exposes runtime,
blueprint, job, graph, event, metric, and run-artifact endpoints and forwards
runtime calls to the core through the Python SDK gRPC client.

## Quick Start

Install locally and run tests:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

Start the local API:

```bash
mn-api
```

Default local URL:

```text
http://localhost:54001
```

## Details

- [MirrorNeuron Component Guide](../mn-docs/component-guide.md#api)
- [API Reference](../mn-docs/api.md)
- [Environment Variables](../mn-docs/env_variables.md)
- [Security Model](../mn-docs/security.md)

## Notes

- A running MirrorNeuron core is required for live runtime calls.
- Use `MN_ENV=prod` with `MN_API_TOKEN` when exposing protected endpoints.
- `MN_RUNS_ROOT` controls where run artifacts are read from.
