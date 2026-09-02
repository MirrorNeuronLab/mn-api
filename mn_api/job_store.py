from __future__ import annotations

from pathlib import Path

from mn_sdk import RuntimeConfig
from mn_sdk.job_store import (
    SAFE_JOB_ID,
    job_data_dir_from_id as sdk_job_data_dir_from_id,
)


def job_data_dir_from_id(job_id: str | None, *, must_exist: bool = True) -> Path | None:
    return sdk_job_data_dir_from_id(job_id, must_exist=must_exist)


def shared_job_ui_dir_from_id(
    job_id: str | None, *, must_exist: bool = True
) -> Path | None:
    """Resolve one job UI handle synchronized through runtime shared storage."""

    if not job_id or not SAFE_JOB_ID.fullmatch(job_id):
        return None
    # This helper runs in the host-side REST API.  ``runtime_shared_storage_root``
    # is the container mount target (for example ``/root/.mn/shared``), whereas
    # this process needs the corresponding host-visible directory.  Using the
    # latter keeps a DockerWorker-created handle readable on Linux hosts whose
    # container and user home paths differ.
    root = Path(RuntimeConfig.from_env().shared_storage_root).expanduser().resolve()
    directory_root = (root / "job-ui").resolve()
    candidate = (directory_root / job_id).resolve()
    try:
        candidate.relative_to(directory_root)
    except ValueError:
        return None
    if must_exist and not candidate.is_dir():
        return None
    return candidate
