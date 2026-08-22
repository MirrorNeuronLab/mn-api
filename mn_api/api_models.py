from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceModel(BaseModel):
    """Sanitized resource projection with resource-specific fields preserved."""

    model_config = ConfigDict(extra="allow")


class PageResponse(StrictModel):
    items: list[ResourceModel]
    next_page_token: str | None = None


class HealthResponse(StrictModel):
    status: Literal["ok"]
    api_contract: Literal["mirrorneuron.rest.v1"]
    auth: Literal["enabled", "disabled"]


class ProblemDetail(StrictModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    request_id: str
    errors: list[dict[str, Any]] | None = None


COMMON_PROBLEM_RESPONSES = {
    code: {"model": ProblemDetail, "description": description}
    for code, description in {
        400: "Invalid request or page token",
        401: "Authentication required",
        403: "Permission denied",
        404: "Resource not found",
        409: "Resource state or idempotency conflict",
        412: "Conditional request failed",
        422: "Request validation failed",
        428: "Conditional request required",
        500: "Unexpected server failure",
        502: "Upstream runtime failure",
        503: "Service unavailable",
    }.items()
}


class JobCreate(StrictModel):
    blueprint_id: str | None = None
    bundle_id: str | None = None
    job_id: str | None = None
    resolved_configuration: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    owner_node: str | None = None


class JobUpdate(StrictModel):
    status: Literal["active", "archived"] | None = None
    display_name: str | None = None
    resolved_configuration: dict[str, Any] | None = None
    storage: dict[str, Any] | None = None


class JobBundleReplacement(StrictModel):
    bundle_id: str


class RunCreate(StrictModel):
    run_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class BlueprintRunCreate(RunCreate):
    job_id: str | None = None
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    secret_environment: dict[str, str] = Field(default_factory=dict)
    force: bool = False
    fake_llm: bool = False
    fake_skills: bool = False
    owner_node: str | None = None


class RunUpdate(StrictModel):
    desired_state: Literal["running", "paused", "cancelled"]


class ScheduleCreate(StrictModel):
    schedule: dict[str, Any]
    source: dict[str, Any] = Field(default_factory=dict)


class ScheduleUpdate(StrictModel):
    desired_state: Literal["running", "paused"] | None = None
    schedule: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    reason: str = ""


class DispatchCreate(StrictModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = "manual"


class TriggerEventCreate(StrictModel):
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"


class HumanResponse(StrictModel):
    response: Any


class HumanAcknowledgement(StrictModel):
    note: str | None = None


class BlueprintValidation(StrictModel):
    config_overrides: dict[str, Any] = Field(default_factory=dict)


class BlueprintAdd(StrictModel):
    force: bool = False


class BlueprintRemove(StrictModel):
    keep_resources: bool = False
    keep_models: bool = False
    remove_models: bool = False
    dry_run: bool = False


class DesiredStateUpdate(StrictModel):
    desired_state: Literal["running", "paused", "failed", "cancelled"]
    reason: str = ""


class DeploymentCreate(StrictModel):
    bundle_id: str
    deployment_key: str = ""
    wait: bool = False
    policy: dict[str, Any] = Field(default_factory=dict)


class DeploymentRollback(StrictModel):
    target_version: str | None = None
    tag: str = ""
    reason: str = ""


class CleanupCreate(StrictModel):
    dry_run: bool = False


class ModelInstallation(StrictModel):
    backend: str = "auto"
    context_size: int | None = None
    force: bool = False


class ModelRegistration(StrictModel):
    model: str
    base_url: str
    name: str | None = None
    api_model: str | None = None
    node: str | None = None
    sync_gateway: bool = False


class ModelProxyRegistration(StrictModel):
    model_id: str
    source_model: str | None = None
    base_url: str = "http://127.0.0.1:4000/v1"
    api_model: str | None = None
    display_name: str | None = None
    container_name: str | None = None
    image: str | None = None
    port: int | None = None
    host: str | None = None
    sync_gateway: bool = False


class ServiceCheck(StrictModel):
    bundle_id: str
    config_overrides: dict[str, Any] = Field(default_factory=dict)


class RuntimeResources(StrictModel):
    cpu: float | None = None
    memory_mb: int | None = None
    gpu: int | None = None
    gpu_memory_mb: int | None = None


class ModelBenchmark(StrictModel):
    prompt: str = "Reply with OK."
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_tokens: int = Field(default=32, ge=1, le=4096)


class NodeCreate(StrictModel):
    host: str
    token: str
    grpc_port: int | None = None


class NodeDrain(StrictModel):
    reason: str = ""
    deadline: str = "30m"
    deadline_ms: int | None = None
    dry_run: bool = False
    wait: bool = False
    ignore_system_jobs: bool = True


class NodeMaintenance(StrictModel):
    maintenance: bool
    reason: str = ""


class ReconciliationCreate(StrictModel):
    reason: str = ""
    dry_run: bool = False
