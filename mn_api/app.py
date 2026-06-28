from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mn_api import state
from mn_api.dependencies import enforce_request_size
from mn_api.routes import blueprints, bundles, deployments, jobs, models, runs, schedules, services, system

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
    app.include_router(schedules.router)
    app.include_router(services.router)
    app.include_router(runs.router)
    return app
