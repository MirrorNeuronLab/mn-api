from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mn_api import state
from mn_api.dependencies import enforce_request_size
from mn_api.errors import app_error_exception_handler, http_exception_handler, unexpected_exception_handler
from mn_sdk.errors import AppError
from mn_api.routes import blueprints, bundles, deployments, jobs, models, realtime, runs, schedules, services, system

INTERFACE_VERSION = 1


class VersionedJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        if isinstance(content, dict) and "version" not in content:
            content = {"version": INTERFACE_VERSION, **content}
        return super().render(content)


def create_app() -> FastAPI:
    app = FastAPI(
        title="MirrorNeuron API",
        version="1.0",
        default_response_class=VersionedJSONResponse,
    )

    app.add_exception_handler(AppError, app_error_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

    if state.config.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=state.config.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.middleware("http")(enforce_request_size)
    app.include_router(system.router)
    app.include_router(blueprints.router)
    app.include_router(bundles.router)
    app.include_router(deployments.router)
    app.include_router(jobs.router)
    app.include_router(models.router)
    app.include_router(realtime.router)
    app.include_router(schedules.router)
    app.include_router(services.router)
    app.include_router(runs.router)
    return app
