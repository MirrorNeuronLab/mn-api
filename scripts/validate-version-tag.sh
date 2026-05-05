#!/usr/bin/env bash
set -euo pipefail

tag="${1:-}"

if [[ -z "$tag" && -n "${GITHUB_REF_NAME:-}" ]]; then
  tag="$GITHUB_REF_NAME"
fi

if [[ -z "$tag" ]]; then
  echo "::error::Missing tag name. Expected a tag such as v1.0.1 or v1.0.1-rc.1." >&2
  exit 1
fi

semver_tag_regex='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(alpha|beta|rc)\.(0|[1-9][0-9]*))?$'

if [[ ! "$tag" =~ $semver_tag_regex ]]; then
  echo "::error::Invalid release tag '$tag'." >&2
  echo "Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N, -beta.N, or -alpha.N." >&2
  echo "Examples: v1.0.1, v1.1.0, v2.0.0, v1.0.1-rc.1" >&2
  exit 1
fi

version="${tag#v}"
is_prerelease="false"

if [[ "$version" == *-* ]]; then
  is_prerelease="true"
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "tag_name=$tag"
    echo "version=$version"
    echo "is_prerelease=$is_prerelease"
  } >> "$GITHUB_OUTPUT"
fi

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "TAG_NAME=$tag"
    echo "VERSION=$version"
    echo "IS_PRERELEASE=$is_prerelease"
  } >> "$GITHUB_ENV"
fi

echo "Validated release tag $tag (version $version, prerelease $is_prerelease)."
