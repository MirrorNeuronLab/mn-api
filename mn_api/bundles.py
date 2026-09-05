from __future__ import annotations

from pathlib import Path
import secrets
import shutil
import tempfile
from typing import Any

from fastapi import HTTPException, UploadFile
from mn_sdk.blueprints import BlueprintError, read_blueprint
from mn_sdk.blueprints.limits import MAX_PACKAGE_BYTES
from mn_sdk.bundle_io import (
    DEFAULT_RAG_NAMESPACE,
    RAG_EXPORT_SCHEMA_VERSION,
    RAG_RUNTIME_ARCHIVE_ROOT,
    BundleError,
    blueprint_id_from_manifest,
    extract_zip_to_dir,
    find_bundle_root,
    first_string,
    load_uploaded_bundle as sdk_load_uploaded_bundle,
    rag_db_sidecar_suffix,
    resolved_rag_db_path,
    restore_exported_rag_db as sdk_restore_exported_rag_db,
    safe_extract_path as sdk_safe_extract_path,
    safe_rag_export_source as sdk_safe_rag_export_source,
    safe_rag_token,
)


def _bundle_http_exception(exc: BundleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def save_uploaded_bundle(
    bundle: UploadFile, upload_root: Path, *, max_bytes: int = MAX_PACKAGE_BYTES
) -> dict[str, Any]:
    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="bundle must be a .zip file")

    max_bytes = min(max_bytes, MAX_PACKAGE_BYTES)
    upload_root.mkdir(parents=True, exist_ok=True)
    bundle_id = secrets.token_urlsafe(24)
    target_dir = upload_root / bundle_id
    target_dir.mkdir(mode=0o700)
    try:
        size = 0
        # Transport bytes live outside the package namespace: an authored asset
        # may itself be named bundle.zip without colliding with this upload.
        with tempfile.NamedTemporaryFile(dir=upload_root, prefix=".upload-", suffix=".zip") as archive:
            while chunk := await bundle.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"Blueprint upload exceeds the {max_bytes} byte limit.")
                archive.write(chunk)
            archive.flush()
            extract_zip_to_dir(Path(archive.name), target_dir)
        bundle_root = find_bundle_root(target_dir)
        read_blueprint(bundle_root)
    except BlueprintError as exc:
        shutil.rmtree(target_dir)
        raise HTTPException(status_code=400, detail={"errors": [issue.to_dict() for issue in exc.issues]}) from exc
    except BundleError as exc:
        shutil.rmtree(target_dir)
        raise _bundle_http_exception(exc) from exc
    except BaseException:
        shutil.rmtree(target_dir)
        raise

    return {"bundle_id": bundle_id}


def load_uploaded_bundle(bundle_id: str, upload_root: Path) -> tuple[str, dict[str, bytes]]:
    target_dir = uploaded_bundle_root(bundle_id, upload_root)
    try:
        return sdk_load_uploaded_bundle(str(target_dir), upload_root)
    except BlueprintError as exc:
        raise HTTPException(status_code=400, detail={"errors": [issue.to_dict() for issue in exc.issues]}) from exc
    except BundleError as exc:
        raise _bundle_http_exception(exc) from exc


def uploaded_bundle_root(bundle_id: str, upload_root: Path) -> Path:
    if not bundle_id or "/" in bundle_id or "\\" in bundle_id or bundle_id in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid bundle_id.")
    root = upload_root.resolve()
    target_dir = (root / bundle_id).resolve()
    if target_dir.parent != root or not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Bundle not found.")
    return find_bundle_root(target_dir)


def safe_extract_path(root: Path, member_name: str) -> Path:
    try:
        return sdk_safe_extract_path(root, member_name)
    except BundleError as exc:
        raise _bundle_http_exception(exc) from exc


def restore_exported_rag_db(
    bundle_root: Path,
    *,
    manifest: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        return sdk_restore_exported_rag_db(bundle_root, manifest=manifest, env=env)
    except BundleError as exc:
        raise _bundle_http_exception(exc) from exc


def safe_rag_export_source(bundle_root: Path, runtime_root: Path, relative_path: str) -> Path | None:
    try:
        return sdk_safe_rag_export_source(bundle_root, runtime_root, relative_path)
    except BundleError as exc:
        raise _bundle_http_exception(exc) from exc


__all__ = [
    "DEFAULT_RAG_NAMESPACE",
    "RAG_EXPORT_SCHEMA_VERSION",
    "RAG_RUNTIME_ARCHIVE_ROOT",
    "blueprint_id_from_manifest",
    "find_bundle_root",
    "first_string",
    "load_uploaded_bundle",
    "uploaded_bundle_root",
    "rag_db_sidecar_suffix",
    "resolved_rag_db_path",
    "restore_exported_rag_db",
    "safe_extract_path",
    "safe_rag_export_source",
    "safe_rag_token",
    "save_uploaded_bundle",
]
