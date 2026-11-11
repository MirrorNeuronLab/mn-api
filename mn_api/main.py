from __future__ import annotations

import uvicorn

from mn_sdk.client_install import is_client_install

if is_client_install():
    from mn_api.client_app import create_client_app, listener

    app = create_client_app()
else:
    from mn_api import state
    from mn_api.app import create_app

    app = create_app()


def start():
    if is_client_install():
        host, port = listener()
    else:
        host, port = state.config.host, state.config.port
        state.logger.info("Starting API server on %s:%s", host, port)
    uvicorn.run("mn_api.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    start()
