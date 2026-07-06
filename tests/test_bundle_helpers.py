from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from mn_api.bundles import (
    RAG_EXPORT_SCHEMA_VERSION,
    RAG_RUNTIME_ARCHIVE_ROOT,
    blueprint_id_from_manifest,
    rag_db_sidecar_suffix,
    resolved_rag_db_path,
    restore_exported_rag_db,
    safe_rag_export_source,
    safe_rag_token,
)


def test_rag_export_helpers_normalize_tokens_and_paths(tmp_path):
    env = {"MN_RAG_DB_ROOT": str(tmp_path / "rag-root")}

    assert safe_rag_token(" ../bad token!! ") == "bad_token"
    assert safe_rag_token("!!!", "fallback") == "fallback"
    assert blueprint_id_from_manifest({"metadata": {"blueprint_id": "meta-bp"}}) == "meta-bp"
    assert blueprint_id_from_manifest({"identity": {"blueprint_id": "identity-bp"}}) == "identity-bp"
    assert resolved_rag_db_path(blueprint_id="bp one", namespace="ns one", env=env) == (
        tmp_path / "rag-root" / "ns_one" / "bp_one.db"
    )
    assert rag_db_sidecar_suffix("bp.db", "bp.db") == ""
    assert rag_db_sidecar_suffix("bp.db-wal", "bp.db") == "-wal"
    assert rag_db_sidecar_suffix("other.db", "bp.db") is None


def test_safe_rag_export_source_rejects_unsafe_paths(tmp_path):
    bundle_root = tmp_path / "bundle"
    runtime_root = bundle_root / RAG_RUNTIME_ARCHIVE_ROOT
    runtime_root.mkdir(parents=True)

    with pytest.raises(HTTPException):
        safe_rag_export_source(bundle_root, runtime_root, "../bad.db")
    with pytest.raises(HTTPException):
        safe_rag_export_source(bundle_root, runtime_root, f"{RAG_RUNTIME_ARCHIVE_ROOT}/../bad.db")

    assert safe_rag_export_source(bundle_root, runtime_root, f"{RAG_RUNTIME_ARCHIVE_ROOT}/missing.db") is None


def test_restore_exported_rag_db_validates_manifest_and_file_names(tmp_path):
    bundle_root = tmp_path / "bundle"
    runtime_root = bundle_root / RAG_RUNTIME_ARCHIVE_ROOT
    runtime_root.mkdir(parents=True)
    export_manifest = runtime_root / "manifest.json"

    export_manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(HTTPException):
        restore_exported_rag_db(bundle_root)

    export_manifest.write_text(
        json.dumps(
            {
                "schema_version": RAG_EXPORT_SCHEMA_VERSION,
                "blueprint_id": "bp",
                "namespace": "ns",
                "files": [{"path": f"{RAG_RUNTIME_ARCHIVE_ROOT}/unexpected.db"}],
            }
        ),
        encoding="utf-8",
    )
    (runtime_root / "unexpected.db").write_text("db", encoding="utf-8")
    with pytest.raises(HTTPException):
        restore_exported_rag_db(bundle_root, env={"MN_RAG_DB_ROOT": str(tmp_path / "rag")})


def test_restore_exported_rag_db_ignores_unknown_schema(tmp_path):
    bundle_root = tmp_path / "bundle"
    runtime_root = bundle_root / RAG_RUNTIME_ARCHIVE_ROOT
    runtime_root.mkdir(parents=True)
    (runtime_root / "manifest.json").write_text(json.dumps({"schema_version": "other"}), encoding="utf-8")

    assert restore_exported_rag_db(bundle_root) == {"imported": False, "path": "", "files": []}
