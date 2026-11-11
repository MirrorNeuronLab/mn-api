"""Foreground application surface. Runtime lifecycle is independently owned."""

import uvicorn
from mn_api import state
from mn_api.app import create_app

app = create_app()


def start():
    uvicorn.run("mn_api.main:app", host=state.config.host, port=state.config.port, reload=False)


if __name__ == "__main__":
    start()
