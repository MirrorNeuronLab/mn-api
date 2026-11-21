from __future__ import annotations

from pathlib import Path

from mn_sdk.runtime_config import (
    default_logs_root as default_logs_root,
    default_runs_root as default_runs_root,
    resolve_mn_home as resolve_mn_home,
)


def inside_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
