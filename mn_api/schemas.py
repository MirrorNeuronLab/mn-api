from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SubmitJobRequest(BaseModel):
    version: int = 1
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")
    force: bool = False


class RestoreJobBackupRequest(BaseModel):
    version: int = 1
    backup_json: str
    bundle_files: Dict[str, str] = Field(default_factory=dict)
    blueprint_id: str = ""
    run_id: str = ""


class BlueprintRunRequest(BaseModel):
    version: int = 1
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
    version: int = 1
    cpu: Optional[int] = None
    gpu: Optional[int] = None
    memory: Optional[int] = None
    disk: Optional[int] = None


class ClusterNodeAddRequest(BaseModel):
    version: int = 1
    host: str
    token: str
    grpc_port: Optional[int] = None


class ClusterNodeRemoveRequest(BaseModel):
    version: int = 1
    node_name: str


class NodeActionRequest(BaseModel):
    version: int = 1
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
    version: int = 1
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")
    schedule: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)


class ScheduleUpdateRequest(BaseModel):
    version: int = 1
    attrs: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class DispatchScheduleRequest(BaseModel):
    version: int = 1
    payload: Dict[str, Any] = Field(default_factory=dict)
    reason: str = "manual"


class EmitEventRequest(BaseModel):
    version: int = 1
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str = "api"


class DeploymentPolicyRequest(BaseModel):
    version: int = 1
    strategy: str = "rolling"
    canary: int = 0
    max_parallel: int = 1
    auto_promote: bool = False
    auto_revert: bool = False


class DeploymentCreateRequest(BaseModel):
    version: int = 1
    manifest_json: Optional[str] = None
    payloads: Optional[Dict[str, str]] = Field(default_factory=dict)
    bundle_path: Optional[str] = Field(default=None, alias="_bundle_path")
    key: str = ""
    wait: bool = False
    policy: DeploymentPolicyRequest = Field(default_factory=DeploymentPolicyRequest)


class DeploymentActionRequest(BaseModel):
    version: int = 1
    reason: str = ""


class DeploymentRollbackRequest(DeploymentActionRequest):
    version: Optional[str] = None
    tag: str = ""


class ModelInstallRequest(BaseModel):
    version: int = 1
    backend: str = "auto"
    context_size: Optional[int] = None
    force: bool = False


class ModelUpdateRequest(BaseModel):
    version: int = 1
    all: bool = False
    force: bool = False


class ModelRemoveRequest(BaseModel):
    version: int = 1
    force: bool = False
