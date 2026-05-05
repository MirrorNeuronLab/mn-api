#!/usr/bin/env bash
set -euo pipefail

tag="${1:-${TAG_NAME:-}}"
output_dir="${2:-dist/release-assets}"

if [[ -z "$tag" ]]; then
  echo "Missing tag name. Pass a tag such as v1.0.1." >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

project_name="${PROJECT_NAME:-$(basename "$repo_root")}"
zip_name="${project_name}-${tag}.zip"

mkdir -p "$output_dir"
output_dir_abs="$(cd "$output_dir" && pwd)"
zip_path="$output_dir_abs/$zip_name"

if [[ -e "$zip_path" ]]; then
  echo "Refusing to overwrite existing release ZIP: $zip_path" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

archive_root="$tmpdir/${project_name}-${tag}"
mkdir -p "$archive_root"

while IFS= read -r -d '' file; do
  case "$file" in
    .git/*|.venv/*|venv/*|env/*|.tox/*|node_modules/*|*/node_modules/*)
      continue
      ;;
    __pycache__/*|*/__pycache__/*|.pytest_cache/*|*/.pytest_cache/*)
      continue
      ;;
    .mypy_cache/*|*/.mypy_cache/*|.ruff_cache/*|*/.ruff_cache/*)
      continue
      ;;
    build/*|*/build/*|dist/*|*/dist/*|htmlcov/*|*/htmlcov/*)
      continue
      ;;
    *.egg-info/*|*/*.egg-info/*|*.pyc|*.pyo|*.tmp|*.swp|.DS_Store|*/.DS_Store)
      continue
      ;;
  esac

  destination="$archive_root/$file"
  mkdir -p "$(dirname "$destination")"
  cp -p "$file" "$destination"
done < <(git ls-files -z)

if [[ -z "$(find "$archive_root" -type f -print -quit)" ]]; then
  echo "No tracked project files were found for the release ZIP." >&2
  exit 1
fi

if touch -h -t 198001010000 "$archive_root" 2>/dev/null; then
  find "$archive_root" -exec touch -h -t 198001010000 {} +
else
  find "$archive_root" -exec touch -t 198001010000 {} +
fi

(
  cd "$tmpdir"
  find "${project_name}-${tag}" -type f | LC_ALL=C sort | zip -X -q "$zip_path" -@
)

echo "$zip_path"
