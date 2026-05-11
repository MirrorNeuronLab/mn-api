from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Dict
import zipfile

from fastapi import HTTPException, UploadFile

from mn_api.path_utils import inside_path


async def save_uploaded_bundle(bundle: UploadFile, upload_root: Path) -> Dict[str, Any]:
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

    return {
        "bundle_path": str(bundle_root),
        "manifest": json.loads(manifest_path.read_text()),
    }


def load_uploaded_bundle(bundle_path: str, upload_root: Path) -> tuple[str, Dict[str, bytes]]:
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

    return manifest_path.read_text(), payloads


def safe_extract_path(root: Path, member_name: str) -> Path:
    member_path = Path(member_name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise HTTPException(status_code=400, detail="bundle contains unsafe paths")

    destination = (root / member_path).resolve()
    if not inside_path(destination, root.resolve()):
        raise HTTPException(status_code=400, detail="bundle contains unsafe paths")
    return destination


def find_bundle_root(extracted_root: Path) -> Path:
    if (extracted_root / "manifest.json").is_file():
        return extracted_root

    children = [path for path in extracted_root.iterdir() if path.is_dir()]
    if len(children) == 1 and (children[0] / "manifest.json").is_file():
        return children[0]

    return extracted_root
