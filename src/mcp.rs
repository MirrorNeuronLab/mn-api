mod access;
mod json;
mod protocol;
mod replies;
mod skills;
mod tools;
use crate::grants::Store;
use crate::{
    auth::{bearer, owner, unauthorized},
    config::Config,
    http::{json_response, origin, send},
    uploads::too_large,
};
pub(crate) use access::admin;
use axum::{
    body::{Body, to_bytes},
    extract::Request,
    response::Response,
};
use mn_client_support::{Error, Result, adapter::Adapter, resources};
use serde_json::{Value, json};
use url::Url;
struct Session {
    ipc: Adapter,
    job: String,
    internal: bool,
    mode: String,
}
impl Session {
    async fn context(&self, method: &str, args: Value, kwargs: Value) -> Result<Value> {
        self.ipc
            .command(
                "mcp.context",
                json!({"job_id":self.job,"method":method,"args":args,"kwargs":kwargs}),
                120,
            )
            .await
    }
    async fn supervision(&self, method: &str, mut fields: Value) -> Result<Value> {
        fields["job_id"] = self.job.clone().into();
        fields["internal"] = self.internal.into();
        fields["method"] = method.into();
        self.ipc.command("mcp.supervision", fields, 120).await
    }
}
fn mcp_context_error(e: Error) -> Error {
    match e.status {
        404 => Error::new(
            404,
            "job_mcp_not_found",
            "The requested co-worker is unavailable.",
        ),
        502..=504 => Error::new(503, "job_mcp_unavailable", &e.message),
        _ => e,
    }
}
pub async fn protocol(c: &Config, r: Request, job: &str) -> Result<Response> {
    let job = resources::decode_id(job)?;
    let auth = if owner(c, r.headers()) {
        Some(true)
    } else {
        Store::open(&c.home)?.authority(&job, bearer(r.headers()))?
    };
    let Some(internal) = auth else {
        let mut e = unauthorized();
        let policy = Store::open(&c.home)?.policy(&job)?;
        let source = policy["server_url"]
            .as_str()
            .and_then(|s| Url::parse(s).ok())
            .map(|u| u.origin().ascii_serialization())
            .unwrap_or_else(|| origin(&r));
        e.headers.insert("www-authenticate".into(),format!("Bearer resource_metadata=\"{source}/.well-known/oauth-protected-resource/api/v1/jobs/{}/mcp\"",resources::resource_id(&job)));
        return Err(e);
    };
    let (ipc, _) = c.ipc.pin().await.map_err(mcp_context_error)?;
    let mut s = Session {
        ipc,
        job,
        internal,
        mode: String::new(),
    };
    s.mode = s
        .context("response_mode", json!([]), json!({}))
        .await
        .map_err(mcp_context_error)?
        .as_str()
        .unwrap_or("base")
        .to_owned();
    if !r.uri().path().ends_with("/mcp") {
        return send(
            404,
            Body::from("Not Found"),
            [("content-type".into(), "text/plain; charset=utf-8".into())],
        );
    }
    if let Some(reply) = protocol::transport(&r)? {
        return Ok(reply);
    }
    let headers = r.headers().clone();
    let current = protocol::current(&headers);
    let body: Value = serde_json::from_slice(
        &to_bytes(r.into_body(), c.limit)
            .await
            .map_err(|_| too_large())?,
    )
    .map_err(|_| Error::invalid("Invalid JSON-RPC request."))?;
    if current && let Some(reply) = protocol::check_envelope(&headers, &body)? {
        return Ok(reply);
    }
    let id = &body["id"];
    if body["jsonrpc"] != "2.0" || !body["method"].is_string() {
        return rpc_error(id, -32600, "Invalid Request", Value::Null);
    }
    if body["method"]
        .as_str()
        .is_some_and(|m| m.starts_with("notifications/"))
        && id.is_null()
    {
        return send(202, Body::empty(), []);
    }
    let p = &body["params"];
    let method = body["method"].as_str().unwrap();
    if method == "tools/call"
        && p["name"] == "ask_job"
        && p["arguments"]["stream"] == true
        && s.mode == "response"
    {
        return replies::stream(s, p.clone(), id.clone()).await;
    }
    let result = match method {
        "initialize" if !current => Ok(protocol::discovery(
            &s.mode,
            p["protocolVersion"].as_str(),
            false,
        )),
        "server/discover" if current => Ok(protocol::discovery(&s.mode, None, true)),
        "ping" if !current => Ok(json!({})),
        "prompts/list" => Ok(json!({"prompts":[]})),
        "tools/list" => {
            let declaration = s.supervision("declaration", json!({})).await?;
            tools::list(&s, &declaration).map(|tools| json!({"tools": tools}))
        }
        "tools/call" => tools::call(&s, p).await,
        "resources/templates/list" => Ok(json!({"resourceTemplates":[]})),
        "skills/list"
        | "skills/get"
        | "resources/read"
        | "resources/list"
        | "resources/directory/read" => skills::resources(&s, method, p).await,
        _ => return protocol::unknown(current, id, method),
    };
    match result {
        Ok(v) => json_response(
            200,
            json!({"jsonrpc":"2.0","id":id,"result":protocol::result(v, &s.mode, method, current)}),
        ),
        Err(e) if e.code == "MN_MCP_DECLARATION_INVALID" && current => {
            protocol::rpc_problem(500, id, -32603, "Internal server error", None)
        }
        Err(e) if e.code == "MN_MCP_DECLARATION_INVALID" => {
            protocol::rpc_problem(200, id, 0, &e.message, None)
        }
        Err(e) if e.message == "Unknown co-worker skill resource." => protocol::rpc_problem(
            if current { 400 } else { 200 },
            id,
            -32602,
            &e.message,
            None,
        ),
        Err(e) => rpc_error(
            id,
            if e.status == 422 { -32602 } else { -32000 },
            &e.message,
            json!({"code":e.code,"retryable":e.status>=500}),
        ),
    }
}
fn rpc_error(id: &Value, code: i64, msg: &str, data: Value) -> Result<Response> {
    json_response(
        200,
        json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":msg,"data":data}}),
    )
}
