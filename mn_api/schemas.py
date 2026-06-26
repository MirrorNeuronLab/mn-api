from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SubmitJobRequest(BaseModel):
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")
    force: bool = False


class RestoreJobBackupRequest(BaseModel):
    backup_json: str
    bundle_files: Dict[str, str] = Field(default_factory=dict)
    blueprint_id: str = ""
    run_id: str = ""


class BlueprintRunRequest(BaseModel):
    run_id: Optional[str] = None
    config_overwrite: Optional[Dict[str, Any]] = None
    config_overrides: Optional[Dict[str, Any]] = None
    force: bool = False
    progress_id: Optional[str] = None


class BlueprintLaunchRequest(BlueprintRunRequest):
    source: str
    blueprint_id: Optional[str] = None
    path: Optional[str] = None
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")


class ResourceSetRequest(BaseModel):
    cpu: Optional[int] = None
    gpu: Optional[int] = None
    memory: Optional[int] = None
    disk: Optional[int] = None


class ClusterNodeAddRequest(BaseModel):
    host: str
    token: str
    grpc_port: Optional[int] = None


class ClusterNodeRemoveRequest(BaseModel):
    node_name: str


class NodeActionRequest(BaseModel):
    reason: str = ""


class NodeReconcileRequest(NodeActionRequest):
    dry_run: bool = False


class NodeDrainRequest(NodeActionRequest):
    deadline: str = "30m"
    deadline_ms: Optional[int] = None
    dry_run: bool = False
    wait: bool = False
    ignore_system_jobs: bool = True


class NodeUndrainRequest(NodeActionRequest):
    mark_eligible: bool = False


class NodeMaintenanceRequest(NodeActionRequest):
    enabled: bool = True


class CreateScheduleRequest(BaseModel):
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")
    schedule: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)


class ScheduleUpdateRequest(BaseModel):
    attrs: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class DispatchScheduleRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    reason: str = "manual"


class EmitEventRequest(BaseModel):
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str = "api"


class DeploymentPolicyRequest(BaseModel):
    strategy: str = "rolling"
    canary: int = 0
    max_parallel: int = 1
    auto_promote: bool = False
    auto_revert: bool = False


class DeploymentCreateRequest(BaseModel):
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")
    key: str = ""
    wait: bool = False
    policy: DeploymentPolicyRequest = Field(default_factory=DeploymentPolicyRequest)


class DeploymentActionRequest(BaseModel):
    reason: str = ""


class DeploymentRollbackRequest(DeploymentActionRequest):
    version: Optional[str] = None
    tag: str = ""


class ModelInstallRequest(BaseModel):
    backend: str = "auto"
    context_size: Optional[int] = None
    force: bool = False


class ModelUpdateRequest(BaseModel):
    all: bool = False
    force: bool = False


class ModelRemoveRequest(BaseModel):
    force: bool = False
