#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -eq 0 ]]; then
  echo "No release artifacts were provided for validation." >&2
  exit 1
fi

for artifact in "$@"; do
  if [[ ! -f "$artifact" ]]; then
    echo "Missing release artifact: $artifact" >&2
    exit 1
  fi

  if [[ ! -s "$artifact" ]]; then
    echo "Release artifact is empty: $artifact" >&2
    exit 1
  fi

  if [[ "$artifact" == *.zip ]]; then
    if unzip -Z1 "$artifact" | grep -E '(^|/)(\.git|node_modules|__pycache__|\.pytest_cache|build|dist)(/|$)' >/dev/null; then
      echo "Release ZIP contains an excluded path: $artifact" >&2
      exit 1
    fi
  fi
done

echo "Release artifacts look valid."
