from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from mn_api import state
from mn_api.api_models import COMMON_PROBLEM_RESPONSES
from mn_api.contracts import API_CONTRACT
from mn_api.dependencies import enforce_request_size
from mn_api.errors import (
    app_error_exception_handler,
    http_exception_handler,
    request_validation_exception_handler,
    unexpected_exception_handler,
)
from mn_api.job_mcp import create_job_mcp, job_mcp_lifespan
from mn_sdk.errors import AppError
from mn_api.routes import bundles
from mn_api.routes.v1 import blueprints, infrastructure, jobs, operations, system


def create_app() -> FastAPI:
    job_mcp_server, job_mcp_app = create_job_mcp()
    app = FastAPI(
        title="MirrorNeuron API",
        version="1.0",
        openapi_url="/api/v1/openapi.json",
        responses=COMMON_PROBLEM_RESPONSES,
        lifespan=job_mcp_lifespan(job_mcp_server),
    )

    app.add_exception_handler(AppError, app_error_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

    if state.config.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=state.config.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-API-Contract"] = API_CONTRACT
        return response

    app.middleware("http")(enforce_request_size)
    app.include_router(system.router)
    app.include_router(blueprints.router)
    app.include_router(bundles.router)
    app.include_router(jobs.router)
    app.include_router(operations.router)
    app.include_router(infrastructure.router)
    app.mount("/api/v1/jobs/{job_id}", job_mcp_app, name="job-mcp")
    return app
