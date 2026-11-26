from __future__ import annotations

from pathlib import Path
from typing import Any
import urllib.parse

from mn_sdk.run_outputs import (
    OUTPUT_CONTENT_TYPES,
    output_content_type,
    output_metadata,
    output_path_by_index,
    output_ref_metadata,
    recorded_output_items,
    recorded_output_path,
    slug,
)


def output_refs(run_id: str, run_dir: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    quoted_run_id = urllib.parse.quote(run_id)
    for index, ref in enumerate(output_metadata(run_dir)):
        decorated = dict(ref)
        decorated.update(
            {
                "url": f"/api/v1/runs/{quoted_run_id}/outputs/{index}",
            }
        )
        refs.append(decorated)
    return refs


_output_ref = output_ref_metadata
_recorded_output_items = recorded_output_items
_recorded_output_path = recorded_output_path
_slug = slug


__all__ = [
    "OUTPUT_CONTENT_TYPES",
    "output_content_type",
    "output_path_by_index",
    "output_refs",
]
