from io import BytesIO
from types import SimpleNamespace
import asyncio

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from mn_api import state
from mn_api.bundles import save_uploaded_bundle
from mn_api.dependencies import enforce_request_size


@pytest.mark.parametrize("path,status", [("/api/v1/bundles", 204), ("/api/v1/jobs", 413)])
def test_blueprint_uploads_have_a_separate_transport_limit(monkeypatch, path, status):
    monkeypatch.setattr(
        state, "config", SimpleNamespace(request_size_limit_bytes=1024, blueprint_upload_limit_bytes=8192)
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", b"4096")],
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
    )

    async def next_handler(_request):
        return Response(status_code=204)

    assert asyncio.run(enforce_request_size(request, next_handler)).status_code == status


def test_actual_upload_bytes_are_capped_and_partial_resources_removed(tmp_path):
    bundle = UploadFile(filename="package.zip", file=BytesIO(b"x" * 11))
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(save_uploaded_bundle(bundle, tmp_path, max_bytes=10))
        assert error.value.status_code == 413
        assert list(tmp_path.iterdir()) == []
    finally:
        bundle.file.close()
