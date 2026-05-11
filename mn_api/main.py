from __future__ import annotations

import uvicorn

from mn_api import state
from mn_api.app import create_app


app = create_app()


def start():
    state.logger.info("Starting API server on %s:%s", state.config.host, state.config.port)
    uvicorn.run("mn_api.main:app", host=state.config.host, port=state.config.port, reload=False)


if __name__ == "__main__":
    start()
