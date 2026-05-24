from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mn_api import state
from mn_api.dependencies import enforce_request_size
from mn_api.routes import blueprints, bundles, jobs, runs, schedules, system


def create_app() -> FastAPI:
    app = FastAPI(title="MirrorNeuron API", version="1.0")

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
    app.include_router(jobs.router)
    app.include_router(schedules.router)
    app.include_router(runs.router)
    return app
