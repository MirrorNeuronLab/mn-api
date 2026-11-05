# Contributing

## Automatic pre-push checks

From this repository root, install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
bash scripts/install-hooks.sh
```

This installs the pinned pre-commit runner and isolated checker dependencies,
and activates `.git/hooks/pre-push` for this checkout. Repeat after a fresh clone.
Git does not activate hooks automatically from tracked files. Existing Git hooks
are preserved by pre-commit's migration mode. No global Git settings are changed.

Each push checks files changed by the outgoing commits, including new branches.
Deletions need no checks. Checks inspect the working tree; commit or stash pending
edits first so the checked content matches what you push. A failure blocks the
push and prints the problem; checks do not auto-format source files.
Generated protobuf bindings and immutable `install_support/` snapshots are excluded.

Run the same checks manually against every tracked file:

```bash
uvx --from pre-commit==4.5.1 pre-commit run --hook-stage pre-push --all-files
```

Tools are pinned in `.pre-commit-config.yaml`. Python uses Ruff lint (including
syntax errors) and format checks. Shell uses Bash syntax, ShellCheck, and shfmt
(two-space indentation). JSON, YAML, and TOML are parsed; Prettier checks document,
web, and data formatting. Existing per-package Ruff configuration remains active.
First use requires network access to populate tool caches. Existing style debt
may fail checks; fix and commit the reported files before pushing.

To remove this checkout’s hook, run `uvx --from pre-commit==4.5.1 pre-commit uninstall --hook-type pre-push`.
