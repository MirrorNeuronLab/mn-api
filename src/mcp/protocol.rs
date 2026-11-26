//! HTTP negotiation and discovery for the two MCP protocols supported by job servers.
use crate::{auth::header_str, http::json_response};
use axum::{
    extract::Request,
    http::{HeaderMap, HeaderValue},
    response::{
        IntoResponse, Response, Sse,
        sse::{Event, KeepAlive},
    },
};
use mn_client_support::Result;
use serde_json::{Value, json};
use std::convert::Infallible;

const LATEST: &str = "2026-07-28";
const HANDSHAKES: [&str; 4] = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];

pub(super) fn current(headers: &HeaderMap) -> bool {
    let version = header_str(headers, "mcp-protocol-version");
    !version.is_empty() && !HANDSHAKES.contains(&version)
}
pub(super) fn rpc_problem(
    status: u16,
    id: &Value,
    code: i32,
    message: &str,
    data: Option<Value>,
) -> Result<Response> {
    let mut error = json!({"code":code,"message":message});
    if let Some(v) = data {
        error["data"] = v;
    }
    json_response(status, json!({"jsonrpc":"2.0","id":id,"error":error}))
}
pub(super) fn check_envelope(headers: &HeaderMap, body: &Value) -> Result<Option<Response>> {
    let version = header_str(headers, "mcp-protocol-version");
    let meta = &body["params"]["_meta"];
    let requested = meta["io.modelcontextprotocol/protocolVersion"].as_str();
    let id = &body["id"];
    let problem = if requested.is_none()
        || !meta["io.modelcontextprotocol/clientCapabilities"].is_object()
    {
        rpc_problem(
            400,
            id,
            -32602,
            "params._meta must be an object carrying the required 'io.modelcontextprotocol/protocolVersion' and 'io.modelcontextprotocol/clientCapabilities' envelope keys",
            None,
        )?
    } else if requested != Some(version) {
        rpc_problem(
            400,
            id,
            -32020,
            "mcp-protocol-version header does not match the request envelope's protocol version",
            None,
        )?
    } else if body["method"].as_str() != Some(header_str(headers, "mcp-method")) {
        rpc_problem(
            400,
            id,
            -32020,
            "mcp-method header does not match the request body's method",
            None,
        )?
    } else if version != LATEST {
        rpc_problem(
            400,
            id,
            -32022,
            "Unsupported protocol version",
            Some(json!({"supported":[LATEST],"requested":version})),
        )?
    } else {
        return Ok(None);
    };
    Ok(Some(problem))
}
fn accepts(headers: &HeaderMap, wanted: &str) -> bool {
    header_str(headers, "accept")
        .split(',')
        .filter_map(|s| s.trim().split(';').next())
        .any(|s| s == "*/*" || s == wanted)
}
pub(super) fn transport(request: &Request) -> Result<Option<Response>> {
    let headers = request.headers();
    let reject = |status, detail| rpc_problem(status, &Value::Null, -32600, detail, None).map(Some);
    match request.method().as_str() {
        "POST" if !accepts(headers, "application/json") => {
            reject(406, "Not Acceptable: Client must accept application/json")
        }
        "POST"
            if header_str(headers, "content-type").split(';').next()
                != Some("application/json") =>
        {
            reject(
                415,
                "Unsupported Media Type: Content-Type must be application/json",
            )
        }
        "POST" => Ok(None),
        "GET" if !accepts(headers, "text/event-stream") => {
            reject(406, "Not Acceptable: Client must accept text/event-stream")
        }
        "GET" => {
            let stream = futures_util::stream::pending::<std::result::Result<Event, Infallible>>();
            let mut r = Sse::new(stream)
                .keep_alive(KeepAlive::new())
                .into_response();
            r.headers_mut()
                .insert("x-accel-buffering", HeaderValue::from_static("no"));
            Ok(Some(r))
        }
        "DELETE" => reject(405, "Method Not Allowed: Session termination not supported"),
        _ => {
            let mut r = rpc_problem(405, &Value::Null, -32600, "Method Not Allowed", None)?;
            r.headers_mut()
                .insert("allow", HeaderValue::from_static("GET, POST, DELETE"));
            Ok(Some(r))
        }
    }
}
fn server(mode: &str) -> (&'static str, &'static str) {
    match mode {
        "response" => (
            "MirrorNeuron real-time job response",
            "Read safe context or ask grounded, multi-turn questions about the job bound by the MCP URL.",
        ),
        "agent" => (
            "MirrorNeuron bounded job agent",
            "Ask the Job's bounded conversational agent for grounded answers, status, explicit simulation control, or explicit learning.",
        ),
        _ => (
            "MirrorNeuron job context",
            "Read-only supervisory context for the job bound by the MCP URL.",
        ),
    }
}

pub(super) fn result(mut result: Value, mode: &str, method: &str, current: bool) -> Value {
    let extension = matches!(
        method,
        "skills/list" | "skills/get" | "resources/directory/read"
    );
    if current || extension {
        if result.get("resultType").is_none() {
            result["resultType"] = "complete".into();
        }
        if method != "tools/call" {
            result["ttlMs"] = 0.into();
            result["cacheScope"] = "private".into();
        }
        if current {
            result["_meta"]["io.modelcontextprotocol/serverInfo"] =
                json!({"name":server(mode).0,"version":""});
        }
    }
    result
}

pub(super) fn discovery(mode: &str, version: Option<&str>, current: bool) -> Value {
    let (name, instructions) = server(mode);
    let server = json!({"name":name,"version":""});
    if current {
        json!({"cacheScope":"private","capabilities":{"extensions":{"io.modelcontextprotocol/skills":{"directoryRead":true}},"prompts":{"listChanged":true},"resources":{"listChanged":true,"subscribe":true},"tools":{"listChanged":true}},"instructions":instructions,"resultType":"complete","supportedVersions":[LATEST],"ttlMs":0,"_meta":{"io.modelcontextprotocol/serverInfo":server}})
    } else {
        let version = version
            .filter(|v| HANDSHAKES.contains(v))
            .unwrap_or("2025-11-25");
        json!({"capabilities":{"experimental":{},"prompts":{"listChanged":false},"resources":{"listChanged":false,"subscribe":false},"tools":{"listChanged":false}},"instructions":instructions,"protocolVersion":version,"serverInfo":server})
    }
}
pub(super) fn unknown(current: bool, id: &Value, method: &str) -> Result<Response> {
    rpc_problem(
        if current { 404 } else { 200 },
        id,
        -32601,
        "Method not found",
        Some(json!(method)),
    )
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standard_and_extension_results_have_their_advertised_cache_fields() {
        let list = json!({"resources":[]});
        assert_eq!(result(list.clone(), "base", "resources/list", false), list);
        let modern = result(list, "agent", "resources/list", true);
        assert_eq!(modern["cacheScope"], "private");
        assert_eq!(
            modern["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            "MirrorNeuron bounded job agent"
        );
        let extension = result(json!({"skills":[]}), "base", "skills/list", false);
        assert_eq!(
            extension,
            json!({"skills":[], "ttlMs":0, "cacheScope":"private", "resultType":"complete"})
        );
    }

    #[test]
    fn handshake_and_discovery_keep_protocol_specific_shapes() {
        let old = discovery("response", Some("unknown"), false);
        assert_eq!(old["protocolVersion"], "2025-11-25");
        assert_eq!(
            old["serverInfo"]["name"],
            "MirrorNeuron real-time job response"
        );
        let new = discovery("agent", None, true);
        assert_eq!(new["supportedVersions"], json!([LATEST]));
        assert_eq!(
            new["_meta"]["io.modelcontextprotocol/serverInfo"]["version"],
            ""
        );
    }
}
