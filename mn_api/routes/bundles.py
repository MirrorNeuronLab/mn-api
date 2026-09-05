from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
import zipfile

from mn_api import state
from mn_api.bundles import save_uploaded_bundle
from mn_api.api_models import ResourceModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth


router = APIRouter(prefix=API_PREFIX, tags=["bundles"])


@router.post(
    "/bundles", status_code=status.HTTP_201_CREATED, operation_id="create_bundle", response_model=ResourceModel
)
async def upload_bundle(response: Response, bundle: UploadFile = File(...), _auth=Depends(require_auth)):
    try:
        result = await save_uploaded_bundle(
            bundle, state.BUNDLE_UPLOAD_ROOT, max_bytes=state.config.blueprint_upload_limit_bytes
        )
        response.headers["Location"] = f"{API_PREFIX}/bundles/{result['bundle_id']}"
        return result
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip bundle") from exc
