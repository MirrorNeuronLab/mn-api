from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable
import uuid

from mn_sdk.errors import AppError

from mn_api.blueprints import find_blueprint, install_blueprint_runtime_models, validate_blueprint_bundle
from mn_api.config import ApiConfig, config_value
from mn_api.path_utils import resolve_mn_home
from mn_api.public import public_value


ProgressReporter = Callable[..., None]


class BlueprintAddError(AppError):
    def __init__(self, *, issues: list[dict[str, str]] | None = None):
        super().__init__(
            "MN_BLUEPRINT_ADD_FAILED",
            "The blueprint could not be added because a runtime prerequisite was not ready.",
            internal_message="Blueprint runtime preparation failed.",
            hint="Review the failed prerequisite, correct it, and add the blueprint again.",
            http_status=500,
        )
        self.operation_issues = issues or []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _blueprint_additions_dir() -> Path:
    configured = config_value("MN_BLUEPRINT_INSTALLS_DIR") or os.getenv("MN_BLUEPRINT_INSTALLS_DIR")
    return Path(configured).expanduser() if configured else resolve_mn_home() / "blueprint_installs"


def blueprint_add_record_path(blueprint_id: str) -> Path:
    return _blueprint_additions_dir() / f"{blueprint_id}.json"


def blueprint_addition(blueprint_id: str) -> dict[str, Any]:
    target = blueprint_add_record_path(blueprint_id)
    if not target.is_file():
        return {"blueprint_id": blueprint_id, "status": "not_added"}
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"blueprint_id": blueprint_id, "status": "added"}
    if not isinstance(record, dict):
        return {"blueprint_id": blueprint_id, "status": "added"}
    return {
        "blueprint_id": blueprint_id,
        "status": "added",
        "added_at": str(record.get("added_at") or record.get("installed_at") or ""),
        "revision": str(record.get("revision") or ""),
    }


def blueprint_public_projection(blueprint: dict[str, Any]) -> dict[str, Any]:
    projected = dict(public_value(blueprint))
    projected.pop("installed", None)
    projected.pop("installation", None)
    addition = blueprint_addition(str(projected.get("id") or projected.get("blueprint_id") or ""))
    projected["added"] = addition["status"] == "added"
    projected["addition"] = addition
    return projected


def _model_issues(model_summary: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for model in model_summary.get("models") or []:
        if not isinstance(model, dict):
            continue
        status = str(model.get("status") or "").strip().lower()
        if status not in {"failed", "error", "incompatible", "unavailable"}:
            continue
        model_id = str(model.get("id") or model.get("model") or model.get("name") or "required runtime model").strip()
        if not model_id or len(model_id) > 160 or os.path.isabs(model_id) or any(ord(char) < 32 for char in model_id):
            model_id = "A required runtime model"
        issues.append(
            {
                "code": "runtime_model_not_ready",
                "message": f"{model_id} could not be prepared for this blueprint.",
                "severity": "error",
            }
        )
    if not issues:
        issues.append(
            {
                "code": "blueprint_runtime_prerequisite_not_ready",
                "message": "A required runtime prerequisite could not be prepared.",
                "severity": "error",
            }
        )
    return issues[:100]


def _write_add_record(
    *,
    config: ApiConfig,
    blueprint_id: str,
    repo_root: Path,
    bundle_root: Path,
    blueprint: dict[str, Any],
    model_summary: dict[str, Any],
) -> Path:
    target = blueprint_add_record_path(blueprint_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        # Runtime tools still consume the original record schema. The public
        # API translates that private compatibility detail into add terminology.
        "schema_version": "mn.blueprint.install.v1",
        "blueprint_id": blueprint_id,
        "name": blueprint.get("name") or blueprint.get("job_name") or blueprint_id,
        "path": blueprint.get("path"),
        "storage_dir": str(repo_root),
        "bundle_root": str(bundle_root),
        "revision": str(blueprint.get("revision") or ""),
        "install_source": config.active_blueprint_location or str(repo_root),
        "installed_at": _utc_now(),
        "models": model_summary.get("models") or [],
    }
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def add_catalog_blueprint(
    config: ApiConfig,
    blueprint_id: str,
    *,
    force: bool = False,
    report_progress: ProgressReporter | None = None,
) -> dict[str, Any]:
    report = report_progress or (lambda **_progress: None)
    report(percent=10, stage="resolve_blueprint", label="Resolve blueprint", detail="Reading the configured blueprint catalog.")
    repo_root, blueprint = find_blueprint(config, blueprint_id)

    report(percent=30, stage="validate_blueprint", label="Validate blueprint", detail="Checking the blueprint bundle and manifest.")
    bundle_root = validate_blueprint_bundle(repo_root, blueprint)

    report(
        percent=50,
        stage="prepare_runtime",
        label="Prepare runtime prerequisites",
        detail="Preparing required models and runtime services.",
    )
    try:
        model_summary = install_blueprint_runtime_models(repo_root, blueprint, force=force)
    except Exception as exc:
        raise BlueprintAddError() from exc
    if not model_summary.get("ok", True):
        raise BlueprintAddError(issues=_model_issues(model_summary))

    report(percent=90, stage="record_addition", label="Record blueprint", detail="Recording the added blueprint locally.")
    _write_add_record(
        config=config,
        blueprint_id=blueprint_id,
        repo_root=repo_root,
        bundle_root=bundle_root,
        blueprint=blueprint,
        model_summary=model_summary,
    )
    return {
        "added": True,
        "blueprint": blueprint_public_projection(blueprint),
        "addition": blueprint_addition(blueprint_id),
        "runtime_preparation": public_value(model_summary),
    }
