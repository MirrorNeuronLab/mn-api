# mn-api

## Rust layout

- Keep `src/main.rs` and binaries under `src/bin/` thin. Start the runtime and call the library entry point.
- Declare modules and export the public interface in `src/lib.rs`. Keep configuration and argument definitions in `src/config.rs` and error types or error presentation in `src/error.rs`.
- Group code by feature. Use the same domain names across client crates (`runs`, `jobs`, `nodes`, `catalog`) wherever they own the same concept. Keep catalog pagination in `catalog.rs`, MCP and OAuth in their respective feature modules, and browser proxying in `web.rs` and `web/`. Shared HTTP response and validation code belongs in `http.rs`, `error.rs`, and `schema.rs`.
- Use `feature.rs` alongside `feature/submodule.rs` for a growing domain. Choose a descriptive name for each submodule and keep helpers private to their feature.
- Put unit tests in a `#[cfg(test)]` module next to the behavior. Use `tests/` for integration tests that drive a public interface or binary.

## Compatibility and verification

Use `../mn-api-old` as the reference for HTTP status, headers, response bodies, validation and streaming. Treat the published OpenAPI document as a contract. Keep client-to-client calls on WIT and runtime calls on the existing gRPC interface.

Run `cargo fmt --check`, `cargo test --locked`, and `cargo clippy --locked --all-targets -- -D warnings` from this crate.
