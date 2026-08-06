from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
import zipfile

from mn_api import state
from mn_api.bundles import save_uploaded_bundle
from mn_api.dependencies import require_auth


router = APIRouter(prefix="/api/v2")


@router.post("/bundles/upload")
async def upload_bundle(bundle: UploadFile = File(...), _auth=Depends(require_auth)):
    try:
        result = await save_uploaded_bundle(bundle, state.BUNDLE_UPLOAD_ROOT)
        try:
            state.client.emit_trigger_event(
                "bundle_uploaded",
                payload={
                    "bundle_path": result.get("bundle_path") or result.get("_bundle_path"),
                    "filename": bundle.filename,
                    "content_type": bundle.content_type,
                },
                source="mn-api",
            )
        except Exception as exc:
            state.logger.warning("bundle upload event emission failed: %s", exc)
        return result
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip bundle") from exc
