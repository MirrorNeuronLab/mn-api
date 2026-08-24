from __future__ import annotations

from pathlib import Path

from mn_sdk.job_store import job_data_dir_from_id as sdk_job_data_dir_from_id


def job_data_dir_from_id(job_id: str | None, *, must_exist: bool = True) -> Path | None:
    return sdk_job_data_dir_from_id(job_id, must_exist=must_exist)
