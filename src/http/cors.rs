//! Credentialed CORS for the owner's configured browser origins.
use axum::{
    body::Body,
    http::{HeaderMap, HeaderValue},
    response::{IntoResponse, Response},
};

const METHODS: &str = "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT";

pub(crate) struct Cors {
    origins: Vec<HeaderValue>,
}
impl Cors {
    pub(crate) fn new(origins: Vec<HeaderValue>) -> Self {
        Self { origins }
    }
    fn allowed(&self, origin: &HeaderValue) -> bool {
        self.origins.iter().any(|v| v == "*" || v == origin)
    }
    pub(crate) fn simple(&self, origin: &HeaderValue, headers: &mut HeaderMap) {
        headers.insert(
            "access-control-allow-credentials",
            HeaderValue::from_static("true"),
        );
        if self.allowed(origin) {
            headers.insert("access-control-allow-origin", origin.clone());
            headers.append("vary", HeaderValue::from_static("Origin"));
        }
    }
    pub(crate) fn preflight(&self, origin: &HeaderValue, headers: &HeaderMap) -> Response {
        let method = headers["access-control-request-method"]
            .to_str()
            .unwrap_or("");
        let mut rejected = Vec::new();
        if !self.allowed(origin) {
            rejected.push("origin");
        }
        if !METHODS.split(", ").any(|v| v == method) {
            rejected.push("method");
        }
        if headers.contains_key("access-control-request-private-network") {
            rejected.push("private-network");
        }
        let mut response = if rejected.is_empty() {
            "OK".into_response()
        } else {
            (
                axum::http::StatusCode::BAD_REQUEST,
                format!("Disallowed CORS {}", rejected.join(", ")),
            )
                .into_response()
        };
        let result = response.headers_mut();
        result.insert(
            "access-control-allow-methods",
            HeaderValue::from_static(METHODS),
        );
        result.insert("access-control-max-age", HeaderValue::from_static("600"));
        self.simple(origin, result);
        result.insert("vary", HeaderValue::from_static("Origin"));
        if let Some(requested) = headers.get("access-control-request-headers") {
            result.insert("access-control-allow-headers", requested.clone());
        }
        response.map(Body::new)
    }
}
