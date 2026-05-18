from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SubmitJobRequest(BaseModel):
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")


class BlueprintRunRequest(BaseModel):
    run_id: Optional[str] = None
    config_overwrite: Optional[Dict[str, Any]] = None
    config_overrides: Optional[Dict[str, Any]] = None


class ResourceSetRequest(BaseModel):
    cpu: Optional[int] = None
    gpu: Optional[int] = None
    memory: Optional[int] = None
