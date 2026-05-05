# Release Process

This repository releases from Git tags. The tag is the public source of truth for a release version.

## Versioning Policy

Use Semantic Versioning tags with a leading `v`:

- `vMAJOR.MINOR.PATCH` for stable releases
- `vMAJOR.MINOR.PATCH-rc.N` for release candidates
- `vMAJOR.MINOR.PATCH-beta.N` or `vMAJOR.MINOR.PATCH-alpha.N` for prereleases

Examples:

- `v1.0.1` = patch release
- `v1.1.0` = minor release
- `v2.0.0` = major release
- `v1.1.0-rc.1` = prerelease

CI build numbers are for internal artifacts only. Public releases use clean SemVer tags, not build-number versions such as `1.0.0-build123`.

## Create a Stable Release

Before creating a release, make sure `main` is clean and tests pass locally.

```bash
git checkout main
git pull
git tag v1.0.1
git push origin v1.0.1
```

Pushing the tag starts the release workflow. The workflow validates the tag, runs tests, builds the project, creates a release ZIP, writes SHA256 checksums, creates a GitHub Release, and uploads the assets.

## Create a Prerelease

```bash
git checkout main
git pull
git tag v1.0.1-rc.1
git push origin v1.0.1-rc.1
```

Prerelease tags create GitHub prereleases. Python prereleases are not published to PyPI by default.

## Python Package Versions

Where this repository contains Python packages, package versions are derived from Git tags at build time. Stable tags such as `v1.0.1` publish package metadata as `1.0.1`, without the leading `v`.

Python package tooling normalizes prerelease metadata to PEP 440, so `v1.0.1-rc.1` becomes a Python package version like `1.0.1rc1`.

## PyPI Trusted Publishing

Python publishing uses PyPI Trusted Publishing with GitHub OIDC. No `PYPI_TOKEN` is required.

To enable PyPI publishing for stable tags:

1. In PyPI, configure a trusted publisher for the `mirrorneuron-api` project. If the project does not exist yet, create a pending publisher for that exact project name.
2. Set the PyPI environment name to `pypi`.
3. Set the workflow filename to `release.yml` (the file lives at `.github/workflows/release.yml`).
4. Leave token-based secrets unset unless there is a deliberate reason to use them.

Stable tags publish to PyPI only after the build and tests succeed. Prerelease tags do not publish to PyPI unless the repository variable `PUBLISH_PRERELEASES_TO_PYPI` is set to `true`.
