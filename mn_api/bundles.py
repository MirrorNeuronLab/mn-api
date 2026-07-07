from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from fastapi import HTTPException, UploadFile
from mn_sdk.bundle_io import (
    DEFAULT_RAG_NAMESPACE,
    RAG_EXPORT_SCHEMA_VERSION,
    RAG_RUNTIME_ARCHIVE_ROOT,
    BundleError,
    blueprint_id_from_manifest,
    extract_zip_to_dir,
    find_bundle_root,
    first_string,
    load_bundle_manifest,
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


async def save_uploaded_bundle(bundle: UploadFile, upload_root: Path) -> dict[str, Any]:
    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="bundle must be a .zip file")

    upload_root.mkdir(parents=True, exist_ok=True)
    target_dir = Path(tempfile.mkdtemp(prefix="bundle_", dir=upload_root))
    archive_path = target_dir / "bundle.zip"
    archive_path.write_bytes(await bundle.read())

    try:
        extract_zip_to_dir(archive_path, target_dir)
        archive_path.unlink(missing_ok=True)
        bundle_root = find_bundle_root(target_dir)
        manifest_path = bundle_root / "manifest.json"
        payloads_path = bundle_root / "payloads"
        if not manifest_path.is_file() or not payloads_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail="bundle zip must contain manifest.json and payloads/",
            )
        manifest = load_bundle_manifest(bundle_root)
        rag_import = restore_exported_rag_db(bundle_root, manifest=manifest)
    except BundleError as exc:
        raise _bundle_http_exception(exc) from exc

    return {
        "bundle_path": str(bundle_root),
        "manifest": manifest,
        "rag_db": rag_import,
    }


def load_uploaded_bundle(bundle_path: str, upload_root: Path) -> tuple[str, dict[str, bytes]]:
    try:
        return sdk_load_uploaded_bundle(bundle_path, upload_root)
    except BundleError as exc:
        raise _bundle_http_exception(exc) from exc


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
    "rag_db_sidecar_suffix",
    "resolved_rag_db_path",
    "restore_exported_rag_db",
    "safe_extract_path",
    "safe_rag_export_source",
    "safe_rag_token",
    "save_uploaded_bundle",
]
