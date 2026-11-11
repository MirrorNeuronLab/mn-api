"""Public REST declarations over the shared SDK runtime client."""

from __future__ import annotations
from mn_api.runtime_adapter import call_runtime
from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from mn_api.api_models import ResourceModel
from mn_api.contracts import API_PREFIX
from mn_api.dependencies import require_auth

router = APIRouter(prefix=API_PREFIX, tags=["bundles"])


@router.post(
    "/bundles", status_code=status.HTTP_201_CREATED, operation_id="create_bundle", response_model=ResourceModel
)
async def upload_bundle(response: Response, bundle: UploadFile = File(...), _auth=Depends(require_auth)):
    """Forward this operation to the connected runtime."""
    return await call_runtime("create_bundle", locals())
