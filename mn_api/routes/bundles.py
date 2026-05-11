from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
import zipfile

from mn_api import state
from mn_api.bundles import save_uploaded_bundle
from mn_api.dependencies import require_auth


router = APIRouter(prefix="/api/v1")


@router.post("/bundles/upload")
async def upload_bundle(bundle: UploadFile = File(...), _auth=Depends(require_auth)):
    try:
        return await save_uploaded_bundle(bundle, state.BUNDLE_UPLOAD_ROOT)
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip bundle") from exc
