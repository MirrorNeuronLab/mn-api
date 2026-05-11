from __future__ import annotations

from pathlib import Path


def inside_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
