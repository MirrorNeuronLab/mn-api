//! Golden responses from the archived FastAPI app, with a fixed correlation ID.
use axum::{
    body::{Body, to_bytes},
    http::{Method, Request, StatusCode},
};
use mn_api::{Config, router};
use serde_json::{Value, json};
use tower::ServiceExt;

async fn request(
    method: Method,
    path: &str,
    body: Option<&str>,
    auth: bool,
) -> (StatusCode, axum::http::HeaderMap, Value) {
    let dir = tempfile::tempdir().unwrap();
    let app = router(Config::new(dir.path().into(), "owner".into()));
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header("host", "testserver")
        .header("x-request-id", "compare");
    if auth {
        request = request.header("authorization", "Bearer owner");
    }
    if body.is_some() {
        request = request.header("content-type", "application/json");
    }
    let reply = app
        .oneshot(
            request
                .body(Body::from(body.unwrap_or("").to_owned()))
                .unwrap(),
        )
        .await
        .unwrap();
    let (parts, body) = reply.into_parts();
    let bytes = to_bytes(body, 4 * 1024 * 1024).await.unwrap();
    let value =
        serde_json::from_slice(&bytes).unwrap_or_else(|_| json!(String::from_utf8_lossy(&bytes)));
    (parts.status, parts.headers, value)
}

#[tokio::test]
async fn authentication_and_routing_match_public_problems() {
    let (status, headers, body) = request(Method::GET, "/api/v1/runs", None, false).await;
    assert_eq!(status, 401);
    assert_eq!(headers["content-type"], "application/problem+json");
    assert_eq!(headers["x-api-contract"], "mirrorneuron.rest.v1");
    assert_eq!(
        body,
        json!({"type":"https://mirrorneuron.io/problems/authentication_required","title":"Authentication required","status":401,"detail":"missing or invalid bearer [redacted]","instance":"/api/v1/runs","code":"authentication_required","request_id":"compare","retryable":false})
    );
    let (status, headers, body) = request(Method::POST, "/api/v1/health", None, false).await;
    assert_eq!(status, 405);
    assert_eq!(headers["allow"], "GET");
    assert_eq!(body["detail"], "Method Not Allowed");
    let (status, headers, _) = request(Method::GET, "/api/v1/runs/?page_size=2", None, true).await;
    assert_eq!(status, 307);
    assert_eq!(
        headers["location"],
        "http://testserver/api/v1/runs?page_size=2"
    );
    assert_eq!(
        request(Method::GET, "/api/v1/unknown", None, false).await.2["detail"],
        "Not Found"
    );
}

#[tokio::test]
async fn validation_reports_each_field_in_dependency_order() {
    let (status, _, body) = request(Method::POST, "/api/v1/runs/run/retries", None, true).await;
    assert_eq!(status, 422);
    assert_eq!(body["detail"], "One or more request fields are invalid.");
    assert_eq!(
        body["errors"],
        json!([
        {"field":"header.Idempotency-Key","code":"missing","message":"Field required"},
        {"field":"","code":"missing","message":"Field required"}])
    );
    let (_, _, body) = request(Method::GET, "/api/v1/runs?page_size=-1", None, true).await;
    assert_eq!(
        body["errors"],
        json!([{"field":"page_size","code":"greater_than_equal","message":"Input should be greater than or equal to 1"}])
    );
    let (_, _, body) = request(Method::POST, "/api/v1/jobs", Some("{"), true).await;
    assert_eq!(
        body["errors"],
        json!([{"field":"1","code":"json_invalid","message":"JSON decode error"}])
    );
}

#[tokio::test]
async fn health_schema_and_documentation_are_public() {
    assert_eq!(
        request(Method::GET, "/api/v1/health", None, false).await.2,
        json!({"auth":"enabled","api_contract":"mirrorneuron.rest.v1","status":"ok"})
    );
    let (status, _, spec) = request(Method::GET, "/api/v1/openapi.json", None, false).await;
    assert_eq!(status, 200);
    assert_eq!(spec["info"]["title"], "MirrorNeuron API");
    for (path, needle) in [
        ("/docs", "SwaggerUIBundle"),
        ("/redoc", "redoc"),
        ("/docs/oauth2-redirect", "swaggerUIRedirectOauth2"),
    ] {
        let (status, h, page) = request(Method::GET, path, None, false).await;
        assert_eq!(status, 200);
        assert_eq!(h["content-type"], "text/html; charset=utf-8");
        assert!(page.as_str().unwrap().contains(needle));
    }
}

#[tokio::test]
async fn browser_preflight_and_early_size_rejection_preserve_headers() {
    let home = tempfile::tempdir().unwrap();
    let app = router(
        Config::new(home.path().into(), String::new())
            .with_cors_origins("*")
            .unwrap(),
    );
    let preflight = Request::builder()
        .method("OPTIONS")
        .uri("/api/v1/jobs")
        .header("origin", "https://browser.test")
        .header("access-control-request-method", "GET")
        .header("access-control-request-headers", "Authorization")
        .header("x-request-id", "check")
        .body(Body::empty())
        .unwrap();
    let response = app.clone().oneshot(preflight).await.unwrap();
    assert_eq!(
        response.headers()["access-control-allow-origin"],
        "https://browser.test"
    );
    assert_eq!(
        response.headers()["access-control-allow-credentials"],
        "true"
    );
    assert_eq!(
        response.headers()["access-control-allow-headers"],
        "Authorization"
    );
    assert_eq!(response.headers()["x-request-id"], "check");
    assert_eq!(
        &to_bytes(response.into_body(), 64).await.unwrap()[..],
        b"OK"
    );
    let big = Request::builder()
        .method("POST")
        .uri("/api/v1/jobs")
        .header("content-length", "6000000")
        .body(Body::empty())
        .unwrap();
    let response = app.oneshot(big).await.unwrap();
    assert_eq!(response.status(), 413);
    let body: Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 2048).await.unwrap()).unwrap();
    assert_eq!(body["request_id"], "");
    assert_eq!(
        body["detail"],
        "The request exceeds the 5242880 byte limit."
    );
}
