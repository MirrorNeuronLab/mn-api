#!/usr/bin/env bash
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
if ! command -v uvx >/dev/null 2>&1; then
  echo "Install uv (https://docs.astral.sh/uv/getting-started/installation/) and rerun bash scripts/install-hooks.sh." >&2
  exit 1
fi
uvx --from pre-commit==4.5.1 pre-commit validate-config
uvx --from pre-commit==4.5.1 pre-commit install --hook-type pre-push --install-hooks
