from __future__ import annotations

from pathlib import Path
from typing import Any
import urllib.parse

from mn_sdk.artifacts import (
    ARTIFACT_CONTENT_TYPES,
    KNOWN_ARTIFACT_IDS,
    artifact_content_type,
    artifact_id,
    artifact_metadata,
    file_sha256,
    list_artifact_files,
    rotated_artifact_id,
)


def artifact_ref(run_id: str, path: Path, run_dir: Path) -> dict[str, Any]:
    ref = artifact_metadata(path, run_dir)
    artifact_path = urllib.parse.quote(ref["relative_path"], safe="/")
    quoted_run_id = urllib.parse.quote(run_id)
    ref.update(
        {
            "url": f"/api/v1/runs/{quoted_run_id}/artifacts/{artifact_path}",
        }
    )
    return ref


__all__ = [
    "ARTIFACT_CONTENT_TYPES",
    "KNOWN_ARTIFACT_IDS",
    "artifact_content_type",
    "artifact_id",
    "artifact_ref",
    "file_sha256",
    "list_artifact_files",
    "rotated_artifact_id",
]
