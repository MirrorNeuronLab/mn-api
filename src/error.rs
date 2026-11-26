//! Public REST problems, matching the archived API and SDK error catalogue.
use crate::http::send;
use axum::{
    body::Body,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use mn_client_support::Error;
use serde_json::{Value, json};
use std::sync::LazyLock;

static CATALOG: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("error/catalog.json")).expect("invalid SDK error catalogue")
});

fn title(code: &str) -> String {
    match code {
        "bad_request" => "Bad request",
        "authentication_required" | "unauthorized" => "Authentication required",
        "permission_denied" => "Permission denied",
        "not_found" => "Resource not found",
        "method_not_allowed" => "Method not allowed",
        "request_timeout" => "Request timed out",
        "conflict" => "Request conflict",
        "precondition_failed" => "Precondition failed",
        "request_too_large" => "Request too large",
        "validation_failed" => "Request validation failed",
        "not_ready" => "Resource not ready",
        "precondition_required" => "Precondition required",
        "rate_limited" => "Too many requests",
        "upstream_failure" => "Upstream service failed",
        "service_unavailable" => "Service unavailable",
        "upstream_timeout" => "Upstream service timed out",
        "internal_error" => "Internal server error",
        "job_mcp_unavailable" => "Job MCP unavailable",
        "job_mcp_not_found" => "Job MCP not found",
        "invalid_content_length" => "Invalid Content-Length",
        other => {
            return other
                .trim_start_matches("MN_")
                .split('_')
                .map(|s| {
                    let mut c = s.chars();
                    c.next()
                        .map(|first| first.to_uppercase().to_string() + &c.as_str().to_lowercase())
                        .unwrap_or_default()
                })
                .collect::<Vec<_>>()
                .join(" ");
        }
    }
    .into()
}

fn public_detail(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| {
                    let sensitive = [
                        "authorization",
                        "bearer",
                        "cookie",
                        "credential",
                        "password",
                        "secret",
                        "token",
                        "api_key",
                    ]
                    .iter()
                    .any(|word| k.to_ascii_lowercase().contains(word));
                    (
                        k.clone(),
                        if sensitive {
                            json!("[redacted]")
                        } else {
                            public_detail(v)
                        },
                    )
                })
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(public_detail).collect()),
        Value::String(text) => {
            static PRIVATE: LazyLock<regex::Regex> = LazyLock::new(|| {
                regex::Regex::new(r"(?i)(bearer\s+|(?:password|secret|token|api_key)=)[^\s&,]+")
                    .unwrap()
            });
            json!(PRIVATE.replace_all(text, "${1}[redacted]"))
        }
        other => other.clone(),
    }
}

pub(crate) fn problem(e: Error, path: &str, id: &str) -> Response {
    let mut v = json!({"type":format!("https://mirrorneuron.io/problems/{}",e.code),"title":title(&e.code),"status":e.status,
        "detail":e.message,"instance":path,"code":e.code,"request_id":id});
    let mut headers = *e.headers;
    if e.code.starts_with("MN_") {
        let spec = &CATALOG[&e.code];
        v["problem_code"] = spec.get(0).cloned().unwrap_or(json!(9000));
        v["category"] = spec.get(1).cloned().unwrap_or(json!("execution"));
        v["retryable"] = spec.get(2).cloned().unwrap_or(json!(false));
        if let Some(h) = e.hint {
            v["hint"] = h.into();
        }
        if e.code.starts_with("MN_OPENSHELL_")
            && let Some(stage) = e.details["stage"].as_str()
            && matches!(
                stage,
                "client_install"
                    | "gateway_setup"
                    | "image_build"
                    | "gateway_connection"
                    | "sandbox_create"
            )
        {
            v["details"] = json!({"stage":stage});
            if let Some(node) = e.details["node"].as_str().filter(|s| {
                s.len() <= 128
                    && s.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_.@:-[]".contains(&c))
            }) {
                v["details"]["node"] = json!(node);
            }
        }
        if let Some(blockers) = e.details.get("blockers") {
            v["details"] = json!({"blockers":public_detail(blockers)});
        }
        if e.details.get("accepted").is_some() {
            let mut details = json!({});
            for field in [
                "accepted",
                "scope",
                "request_id",
                "command_id",
                "retry_after_ms",
            ] {
                if let Some(value) = e.details.get(field) {
                    details[field] = value.clone();
                }
            }
            if let Some(ms) = details["retry_after_ms"].as_u64().filter(|ms| *ms > 0) {
                headers.insert("retry-after".into(), ms.div_ceil(1000).to_string());
            }
            v["details"] = details;
        }
    } else if let Some(errors) = e.details.get("errors") {
        v["errors"] = errors.clone();
    } else if matches!(
        e.code.as_str(),
        "not_found"
            | "authentication_required"
            | "permission_denied"
            | "bad_request"
            | "method_not_allowed"
            | "conflict"
            | "precondition_failed"
            | "validation_failed"
            | "request_failed"
            | "service_unavailable"
    ) {
        v["retryable"] = (e.status >= 500 || matches!(e.status, 408 | 425 | 429)).into();
    }
    headers.insert("content-type".into(), "application/problem+json".into());
    send(e.status, Body::from(v.to_string()), headers)
        .unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
}
