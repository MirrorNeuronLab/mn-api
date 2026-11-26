# mn-api

The production Axum HTTP API runs as `mn-api.wasm` under host Wasmtime. The Rust package also builds the optional `mn-web-ui-server` static-site and job UI proxy. The API exposes the v1 REST resources, MCP, OAuth and job WebSockets. Its calls to `mn-client-support` use generated WIT bindings and a process-private authenticated connection to the support component.

Keep the matching support crate beside this checkout for `cargo build --locked --release`. Install the sibling repositories with `mn-deploy/install.sh --no-runtime`. Start `mn-api` in the foreground. Its default address is `localhost:54001`. The health endpoint is `/api/v1/health`, and the OpenAPI document is `/api/v1/openapi.json`. The API reads its bearer credential from `MN_API_TOKEN` or `MN_HOME/api.token`.

For an existing browser build, set `MN_WEB_UI_DIST_DIR` to the directory containing `index.html` and run `mn-web-ui-server`. Its default address is `localhost:55173`. The web process forwards same-origin requests to the API.
