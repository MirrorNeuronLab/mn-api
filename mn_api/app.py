from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from mn_sdk_shared.errors import AppError
from mn_sdk_shared.runtime_contract.files import BINDING_HEADER
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
from mn_api.mcp_oauth import OAuthApp, router as oauth_router
from mn_api.routes import bundles
from mn_api.routes.v1 import (
    assistance,
    blueprints,
    infrastructure,
    interactions,
    job_backups,
    job_preparations,
    job_integrations,
    job_ui_relay,
    jobs,
    mailbox,
    mcp_access,
    operations,
    run_observations,
    runs,
    runtime_signals,
    schedules,
    system,
)


def create_app() -> FastAPI:
    job_mcp_servers, job_mcp_app = create_job_mcp()
    mcp_lifespan = job_mcp_lifespan(job_mcp_servers)

    @asynccontextmanager
    async def runtime_lifespan(app: FastAPI):
        async with mcp_lifespan(app):
            yield

    app = FastAPI(
        title="MirrorNeuron API",
        version="1.0",
        openapi_url="/api/v1/openapi.json",
        responses=COMMON_PROBLEM_RESPONSES,
        lifespan=runtime_lifespan,
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
        if binding := getattr(request.state, "run_file_binding", None):
            response.headers[BINDING_HEADER] = binding
        return response

    app.middleware("http")(enforce_request_size)
    app.include_router(system.router)
    app.include_router(blueprints.router)
    app.include_router(bundles.router)
    app.include_router(jobs.router)
    app.include_router(runs.router)
    app.include_router(schedules.router)
    app.include_router(run_observations.router)
    app.include_router(job_ui_relay.router)
    app.include_router(job_backups.router)
    app.include_router(job_preparations.router)
    app.include_router(job_integrations.router)
    app.include_router(operations.router)
    app.include_router(runtime_signals.router)
    app.include_router(interactions.router)
    app.include_router(assistance.router)
    app.include_router(infrastructure.router)
    app.include_router(mailbox.router)
    app.include_router(mcp_access.router)
    app.include_router(oauth_router)
    app.mount("/api/v1/mcp/oauth", OAuthApp(), name="mcp-oauth")
    app.mount("/api/v1/jobs/{job_id}", job_mcp_app, name="job-mcp")
    return app
