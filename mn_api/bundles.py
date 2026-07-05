from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
import zipfile

from fastapi import HTTPException, UploadFile
from mn_sdk import expand_manifest_json_if_source, is_manifest_source, expand_manifest_source

from mn_api.path_utils import inside_path, resolve_mn_home


RAG_RUNTIME_ARCHIVE_ROOT = "__mn_runtime/rag"
RAG_EXPORT_SCHEMA_VERSION = "mn.rag_export.v1"
DEFAULT_RAG_NAMESPACE = "mirror_neuron_rag"


async def save_uploaded_bundle(bundle: UploadFile, upload_root: Path) -> dict[str, Any]:
    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="bundle must be a .zip file")

    upload_root.mkdir(parents=True, exist_ok=True)
    target_dir = Path(tempfile.mkdtemp(prefix="bundle_", dir=upload_root))
    archive_path = target_dir / "bundle.zip"
    archive_path.write_bytes(await bundle.read())

    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            destination = safe_extract_path(target_dir, member.filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                destination.write_bytes(source.read())

    archive_path.unlink(missing_ok=True)
    bundle_root = find_bundle_root(target_dir)
    manifest_path = bundle_root / "manifest.json"
    payloads_path = bundle_root / "payloads"

    if not manifest_path.is_file() or not payloads_path.is_dir():
        raise HTTPException(
            status_code=400,
            detail="bundle zip must contain manifest.json and payloads/",
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="bundle manifest.json is malformed") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="bundle manifest.json must be an object")
    if is_manifest_source(manifest):
        manifest = expand_manifest_source(manifest, root_dir=bundle_root)

    rag_import = restore_exported_rag_db(bundle_root, manifest=manifest)

    return {
        "bundle_path": str(bundle_root),
        "manifest": manifest,
        "rag_db": rag_import,
    }


def load_uploaded_bundle(bundle_path: str, upload_root: Path) -> tuple[str, dict[str, bytes]]:
    bundle_root = Path(bundle_path).resolve()
    root = upload_root.resolve()
    if not inside_path(bundle_root, root) or not bundle_root.is_dir():
        raise HTTPException(status_code=400, detail="unknown uploaded bundle")

    manifest_path = bundle_root / "manifest.json"
    payloads_path = bundle_root / "payloads"
    if not manifest_path.is_file() or not payloads_path.is_dir():
        raise HTTPException(status_code=400, detail="invalid uploaded bundle")

    payloads = {}
    for path in payloads_path.rglob("*"):
        if path.is_file():
            payloads[path.relative_to(payloads_path).as_posix()] = path.read_bytes()

    manifest_json = manifest_path.read_text(encoding="utf-8")
    manifest_json = expand_manifest_json_if_source(manifest_json, root_dir=bundle_root)
    return manifest_json, payloads


def safe_extract_path(root: Path, member_name: str) -> Path:
    member_path = Path(member_name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise HTTPException(status_code=400, detail="bundle contains unsafe paths")

    destination = (root / member_path).resolve()
    if not inside_path(destination, root.resolve()):
        raise HTTPException(status_code=400, detail="bundle contains unsafe paths")
    return destination


def restore_exported_rag_db(
    bundle_root: Path,
    *,
    manifest: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    runtime_root = bundle_root / RAG_RUNTIME_ARCHIVE_ROOT
    export_manifest_path = runtime_root / "manifest.json"
    if not export_manifest_path.is_file():
        return {"imported": False, "path": "", "files": []}

    try:
        export_manifest = json.loads(export_manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="bundle RAG export manifest is malformed") from exc
    if not isinstance(export_manifest, dict):
        raise HTTPException(status_code=400, detail="bundle RAG export manifest must be an object")
    if export_manifest.get("schema_version") != RAG_EXPORT_SCHEMA_VERSION:
        return {"imported": False, "path": "", "files": []}

    blueprint_id = safe_rag_token(
        first_string(
            export_manifest.get("blueprint_id"),
            blueprint_id_from_manifest(manifest),
        ),
        "",
    )
    if not blueprint_id:
        raise HTTPException(status_code=400, detail="bundle RAG export manifest is missing blueprint_id")
    namespace = safe_rag_token(export_manifest.get("namespace"), DEFAULT_RAG_NAMESPACE)
    destination = resolved_rag_db_path(blueprint_id=blueprint_id, namespace=namespace, env=env)
    destination.parent.mkdir(parents=True, exist_ok=True)

    copied_files: list[str] = []
    expected_name = f"{blueprint_id}.db"
    for file_record in export_manifest.get("files") or []:
        if not isinstance(file_record, dict):
            continue
        relative_path = first_string(file_record.get("path"))
        if not relative_path:
            continue
        source = safe_rag_export_source(bundle_root, runtime_root, relative_path)
        if source is None:
            continue
        suffix = rag_db_sidecar_suffix(source.name, expected_name)
        if suffix is None:
            raise HTTPException(status_code=400, detail="bundle RAG export manifest references an unexpected DB file")
        target = Path(f"{destination}{suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_files.append(str(target))

    return {
        "imported": bool(copied_files),
        "path": str(destination) if copied_files else "",
        "files": copied_files,
    }


def safe_rag_export_source(bundle_root: Path, runtime_root: Path, relative_path: str) -> Path | None:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    if not normalized.startswith(f"{RAG_RUNTIME_ARCHIVE_ROOT}/") or ".." in Path(normalized).parts:
        raise HTTPException(status_code=400, detail="bundle RAG export manifest contains unsafe paths")
    source = (bundle_root / normalized).resolve()
    runtime = runtime_root.resolve()
    if not inside_path(source, runtime):
        raise HTTPException(status_code=400, detail="bundle RAG export manifest contains unsafe paths")
    return source if source.is_file() else None


def rag_db_sidecar_suffix(file_name: str, expected_name: str) -> str | None:
    if file_name == expected_name:
        return ""
    for suffix in ("-shm", "-wal"):
        if file_name == f"{expected_name}{suffix}":
            return suffix
    return None


def resolved_rag_db_path(
    *,
    blueprint_id: str,
    namespace: str = DEFAULT_RAG_NAMESPACE,
    env: dict[str, str] | None = None,
) -> Path:
    values = env if env is not None else os.environ
    configured_root = first_string(values.get("MN_RAG_DB_ROOT"))
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
    else:
        root = (resolve_mn_home(values) / "rag").expanduser().resolve()
    return root / safe_rag_token(namespace, DEFAULT_RAG_NAMESPACE) / f"{safe_rag_token(blueprint_id)}.db"


def blueprint_id_from_manifest(manifest: dict[str, Any] | None) -> str:
    if not isinstance(manifest, dict):
        return ""
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    identity = manifest.get("identity") if isinstance(manifest.get("identity"), dict) else {}
    return first_string(
        metadata.get("blueprint_id"),
        identity.get("blueprint_id"),
        manifest.get("blueprint_id"),
        manifest.get("id"),
    )


def safe_rag_token(value: Any, fallback: str = "default") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", first_string(value, fallback))
    normalized = normalized.strip("._-")
    return normalized or fallback


def first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def find_bundle_root(extracted_root: Path) -> Path:
    if (extracted_root / "manifest.json").is_file():
        return extracted_root

    children = [path for path in extracted_root.iterdir() if path.is_dir()]
    if len(children) == 1 and (children[0] / "manifest.json").is_file():
        return children[0]

    return extracted_root
