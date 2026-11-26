mod grants;
mod mcp;
mod oauth;
mod schema;
mod ui;
use axum::{
    Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{HeaderMap, HeaderName, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use clap::Parser;
use futures_util::{StreamExt, TryStreamExt};
use mn_client_support::{
    Error, Result,
    adapter::Adapter,
    connection,
    control::{Chunks, Method, Reply},
    resources,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Instant,
};
use subtle::ConstantTimeEq;
use uuid::Uuid;

#[derive(Parser)]
#[command(version, about = "MirrorNeuron REST API")]
struct Options {
    #[arg(long, env = "MN_API_HOST", default_value = "localhost")]
    host: String,
    #[arg(long, env = "MN_API_PORT", default_value_t = 54001)]
    port: u16,
    #[arg(long,env="MN_API_REQUEST_SIZE_LIMIT_BYTES",default_value_t=5*1024*1024)]
    request_limit: usize,
    #[arg(long,env="MN_API_BLUEPRINT_UPLOAD_LIMIT_BYTES",default_value_t=32*1024*1024*1024)]
    upload_limit: usize,
}
struct Cursor {
    category: Option<String>,
    ids: Vec<String>,
    offset: usize,
    expires: Instant,
}
struct Config {
    ipc: Adapter,
    home: PathBuf,
    token: String,
    limit: usize,
    upload: usize,
    cursors: Mutex<HashMap<String, Cursor>>,
}
type Shared = Arc<Config>;
#[tokio::main]
async fn main() -> std::process::ExitCode {
    if let Err(e) = start().await {
        eprintln!("mn-api: {e}");
        return std::process::ExitCode::FAILURE;
    }
    std::process::ExitCode::SUCCESS
}
async fn start() -> Result<()> {
    let o = Options::parse();
    let home = connection::home()?;
    let token = std::env::var("MN_API_TOKEN")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::fs::read_to_string(home.join("api.token")).ok())
        .unwrap_or_default()
        .trim()
        .to_owned();
    if token.is_empty()
        && (connection::role(&home)? == "client"
            || matches!(
                std::env::var("MN_ENV").as_deref(),
                Ok("prod" | "production")
            ))
    {
        return Err(Error::new(
            503,
            "MN_CLIENT_AUTH_REQUIRED",
            "Configure MN_API_TOKEN or a private MN_HOME/api.token file.",
        ));
    }
    if o.port == 0
        || o.request_limit == 0
        || !(1..=32 * 1024 * 1024 * 1024).contains(&o.upload_limit)
    {
        return Err(Error::invalid("Invalid API port or body size limit."));
    }
    let config = Arc::new(Config {
        ipc: Adapter::new(&home),
        home,
        token,
        limit: o.request_limit,
        upload: o.upload_limit,
        cursors: Default::default(),
    });
    let mut app = Router::new().fallback(dispatch).with_state(config);
    if let Ok(origins) = std::env::var("MN_API_CORS_ALLOW_ORIGINS")
        && !origins.is_empty()
    {
        use tower_http::cors::{Any, CorsLayer};
        let layer = CorsLayer::new().allow_methods(Any).allow_headers(Any);
        let origins = origins
            .split(',')
            .map(|s| {
                s.trim()
                    .parse::<HeaderValue>()
                    .map_err(|_| Error::invalid("Invalid CORS origin."))
            })
            .collect::<Result<Vec<_>>>()?;
        app = app.layer(if origins.iter().any(|v| v == "*") {
            layer.allow_origin(Any)
        } else {
            layer.allow_origin(origins)
        });
    }
    let listener = tokio::net::TcpListener::bind((o.host.as_str(), o.port))
        .await
        .map_err(|_| Error::invalid("Cannot bind the configured API address."))?;
    eprintln!(
        "mn-api listening on {}",
        listener.local_addr().map_err(|_| Error::storage())?
    );
    axum::serve(listener, app)
        .await
        .map_err(|_| Error::storage())
}
fn header_str<'a>(h: &'a HeaderMap, k: &str) -> &'a str {
    h.get(k).and_then(|h| h.to_str().ok()).unwrap_or_default()
}
fn bearer(h: &HeaderMap) -> &str {
    header_str(h, "authorization")
        .split_once(' ')
        .filter(|(s, _)| s.eq_ignore_ascii_case("bearer"))
        .map(|(_, s)| s)
        .unwrap_or_default()
}
fn equal(a: &str, b: &str) -> bool {
    a.as_bytes().ct_eq(b.as_bytes()).into()
}
fn owner(c: &Config, h: &HeaderMap) -> bool {
    !c.token.is_empty() && equal(bearer(h), &c.token)
}
fn unauthorized() -> Error {
    Error::new(401, "unauthorized", "Missing or invalid bearer token.")
}
fn origin(r: &Request) -> String {
    format!("http://{}", header_str(r.headers(), "host"))
}
fn query(s: &str) -> Value {
    Value::Object(
        url::form_urlencoded::parse(s.as_bytes())
            .map(|(k, v)| (k.into_owned(), json!(v)))
            .collect(),
    )
}
fn send(
    status: u16,
    body: Body,
    headers: impl IntoIterator<Item = (String, String)>,
) -> Result<Response> {
    let mut r = Response::new(body);
    *r.status_mut() = StatusCode::from_u16(status).map_err(|_| Error::protocol())?;
    for (k, v) in headers {
        if matches!(
            k.to_ascii_lowercase().as_str(),
            "connection"
                | "transfer-encoding"
                | "keep-alive"
                | "upgrade"
                | "proxy-authenticate"
                | "proxy-authorization"
        ) {
            continue;
        }
        r.headers_mut().append(
            HeaderName::try_from(&k).map_err(|_| Error::protocol())?,
            HeaderValue::from_str(&v).map_err(|_| Error::protocol())?,
        );
    }
    Ok(r)
}
fn json_response(status: u16, v: Value) -> Result<Response> {
    send(
        status,
        Body::from(v.to_string()),
        [("content-type".into(), "application/json".into())],
    )
}
fn response(r: Reply) -> Result<Response> {
    let mut headers = r.headers;
    let body = if r.has_body && r.status != 204 {
        headers
            .entry("content-type".into())
            .or_insert("application/json".into());
        Body::from(r.value.to_string())
    } else {
        Body::empty()
    };
    send(r.status, body, headers)
}
fn problem(e: Error, path: &str, id: &str) -> Response {
    let mut v = json!({"type":format!("https://mirrorneuron.io/problems/{}",e.code),"title":StatusCode::from_u16(e.status).ok().and_then(|s|s.canonical_reason()).unwrap_or("Request failed"),"detail":e.message,"status":e.status,"code":e.code,"request_id":id,"instance":path});
    if let Some(h) = e.hint {
        v["hint"] = h.into();
    }
    if let Some(errors) = e.details.get("errors") {
        v["errors"] = errors.clone();
    }
    let mut headers = *e.headers;
    headers.insert("content-type".into(), "application/problem+json".into());
    send(e.status, Body::from(v.to_string()), headers)
        .unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response())
}
async fn dispatch(State(c): State<Shared>, r: Request) -> Response {
    let p = r.uri().path().to_owned();
    let id = r
        .headers()
        .get("x-request-id")
        .filter(|v| !v.is_empty() && v.len() <= 255)
        .cloned()
        .unwrap_or_else(|| Uuid::new_v4().to_string().parse().unwrap());
    let mut response = match route(&c, r, &p).await {
        Ok(r) => r,
        Err(e) => problem(e, &p, id.to_str().unwrap_or_default()),
    };
    response.headers_mut().insert("x-request-id", id);
    response.headers_mut().insert(
        "x-api-contract",
        HeaderValue::from_static("mirrorneuron.rest.v1"),
    );
    response
}
async fn route(c: &Shared, r: Request, path: &str) -> Result<Response> {
    let m = r.method().as_str().to_owned();
    if m == "GET" && matches!(path, "/docs" | "/redoc") {
        let page = if path == "/docs" {
            r#"<!doctype html><title>MirrorNeuron API</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"><div id="swagger-ui"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:'/api/v1/openapi.json',dom_id:'#swagger-ui'});</script>"#
        } else {
            r#"<!doctype html><title>MirrorNeuron API</title><redoc spec-url="/api/v1/openapi.json"></redoc><script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>"#
        };
        return Ok(axum::response::Html(page).into_response());
    }
    if path.starts_with("/.well-known/") || path.starts_with("/api/v1/mcp/oauth/") {
        return oauth::route(c, r, path).await;
    }
    let path = path
        .strip_prefix("/api/v1/")
        .map(|p| format!("/{p}"))
        .ok_or_else(|| Error::new(404, "not_found", "Resource not found."))?;
    if m == "GET" && path == "/health" {
        return json_response(
            200,
            json!({"status":"ok","api_contract":"mirrorneuron.rest.v1","auth":if c.token.is_empty(){"disabled"}else{"enabled"}}),
        );
    }
    if m == "GET" && path == "/openapi.json" {
        return send(
            200,
            Body::from(schema::SPEC),
            [("content-type".into(), "application/json".into())],
        );
    }
    let segments: Vec<_> = path.trim_start_matches('/').split('/').collect();
    if let ["jobs", job, "mcp"] = segments.as_slice() {
        return mcp::protocol(c, r, job).await;
    }
    if !c.token.is_empty() && !owner(c, r.headers()) {
        return Err(unauthorized());
    }
    if let ["jobs", job, "ui-proxy", port, tail @ ..] = segments.as_slice() {
        return ui::relay(c, r, job, port, &tail.join("/")).await;
    }
    if let ["jobs", job, kind @ ("mcp-access" | "mcp-sessions")] = segments.as_slice() {
        return mcp::admin(c, r, job, kind).await;
    }
    let raw_query = r.uri().query().unwrap_or_default().to_string();
    let mut q = schema::query(&raw_query);
    let mut headers = HashMap::new();
    for (k, v) in r.headers() {
        if let Ok(v) = v.to_str() {
            headers.insert(k.to_string(), v.to_owned());
        }
    }
    if path == "/blueprints" && m == "GET" {
        schema::parameters(
            schema::operation("/blueprints", "get"),
            &mut json!({}),
            &mut q,
            &headers,
        )?;
        return list_blueprints(c, q).await;
    }
    let (ep, mut variables) = resources::resolve(&m, &path)?;
    let op = schema::operation(&ep.path, &m);
    schema::parameters(op, &mut variables, &mut q, &headers)?;
    let action = ep.action.as_str();
    let upload = matches!(action, "create_bundle" | "restore_job_backup");
    let limit = match action {
        "create_bundle" => c.upload + 1024 * 1024,
        "restore_job_backup" => 128 * 1024 * 1024 * 1024,
        _ => c.limit,
    };
    if headers
        .get("content-length")
        .is_some_and(|s| s.parse::<u64>().is_ok_and(|n| n > limit as u64))
    {
        return Err(too_large());
    }
    let base = origin(&r);
    let context_headers: HashMap<_, _> = headers
        .iter()
        .filter(|(k, _)| {
            matches!(
                k.as_str(),
                "range"
                    | "if-range"
                    | "if-none-match"
                    | "accept"
                    | "content-type"
                    | "last-event-id"
                    | "x-request-id"
                    | "x-mn-run-file-binding"
            )
        })
        .collect();
    let context = json!({"headers":context_headers,"method":m,"path":format!("/api/v1{path}"),"query":raw_query});
    let (body, chunks, filename) = if upload {
        let (chunks, name) = upload_body(action, r, limit, c.upload).await?;
        (None, Some(chunks), name)
    } else {
        let bytes = to_bytes(r.into_body(), limit)
            .await
            .map_err(|_| too_large())?;
        let body = if bytes.is_empty() {
            None
        } else {
            Some(serde_json::from_slice(&bytes).map_err(|_| {
                Error::new(422, "input_validation_failed", "Invalid JSON request body.")
            })?)
        };
        schema::body(op, body.as_ref())?;
        (body, None, None)
    };
    let mut args = ep.arguments(
        &variables,
        &q,
        &headers,
        body.as_ref(),
        if c.token.is_empty() {
            "anonymous"
        } else {
            "authenticated"
        },
    );
    match action {
        "create_node" => {
            let v = &args["request"];
            let port = match v["grpc_port"].as_i64() {
                Some(n) => {
                    Some(u16::try_from(n).map_err(|_| Error::invalid("Invalid gRPC port."))?)
                }
                None => None,
            };
            let result = c
                .ipc
                .connect(
                    v["host"].as_str().unwrap_or_default(),
                    v["token"].as_str(),
                    port,
                    v["runtime_home"].as_str(),
                )
                .await?;
            let mut r = json_response(201, result.clone())?;
            let node = result["node_name"]
                .as_str()
                .or_else(|| result.pointer("/runtime/node_name").and_then(Value::as_str))
                .unwrap_or_default();
            r.headers_mut().insert(
                header::LOCATION,
                format!("/api/v1/nodes/{}", resources::resource_id(node))
                    .parse()
                    .map_err(|_| Error::protocol())?,
            );
            return Ok(r);
        }
        "delete_node" => {
            return json_response(
                200,
                c.ipc
                    .remove(variables["node_id"].as_str().unwrap_or_default())
                    .await?,
            );
        }
        _ => {}
    }
    if let Some(chunks) = chunks {
        args["_stream"] = context;
        if let Some(f) = filename {
            args["_filename"] = f.into();
        }
        return response(c.ipc.upload(action, args, chunks).await?);
    }
    let method = ep.rpc();
    if matches!(method, Method::Events | Method::ReadFile) {
        args["_stream"] = context;
        let (reply, data) = c.ipc.download(method, action, args).await?;
        return send(reply.status, Body::from_stream(data), reply.headers);
    }
    args["_context"] = context;
    let id = args["idempotency_key"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let timeout = ep.deadline(&args);
    let mut reply = c.ipc.call(method, action, args, &id, timeout).await?;
    if action == "get_job_ui"
        && let Some(transport) = reply
            .value
            .pointer_mut("/web_ui/transport")
            .filter(|v| v.is_object())
    {
        transport["owner_api_url"] = format!("{base}/api/v1").into();
    }
    response(reply)
}
fn too_large() -> Error {
    Error::new(
        413,
        "request_too_large",
        "The request exceeds the configured size limit.",
    )
}
async fn upload_body(
    action: &str,
    r: Request,
    limit: usize,
    file_limit: usize,
) -> Result<(Chunks, Option<String>)> {
    use http_body_util::Limited;
    let ct = header_str(r.headers(), "content-type").to_owned();
    let raw = Body::new(Limited::new(r.into_body(), limit))
        .into_data_stream()
        .map_err(|_| too_large());
    if action == "restore_job_backup" {
        if ct.split(';').next() != Some("application/zip") {
            return Err(Error::new(
                415,
                "unsupported_media_type",
                "Send the backup with Content-Type application/zip.",
            ));
        }
        return Ok((Box::pin(raw), None));
    }
    let boundary = multer::parse_boundary(&ct)
        .map_err(|_| Error::invalid("Expected a multipart bundle upload."))?;
    let mut multipart = multer::Multipart::new(raw, boundary);
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| Error::invalid("Invalid multipart upload."))?
    {
        if field.name() == Some("bundle") {
            let name = field.file_name().map(str::to_owned);
            let mut size = 0;
            let chunks = field.map(move |item| {
                let b = item.map_err(|_| Error::invalid("Interrupted multipart upload."))?;
                size += b.len();
                if size > file_limit {
                    Err(too_large())
                } else {
                    Ok(b)
                }
            });
            return Ok((Box::pin(chunks), name));
        }
    }
    Err(Error::invalid("Missing bundle file."))
}
async fn list_blueprints(c: &Config, q: Value) -> Result<Response> {
    let cat = q["category"].as_str();
    let list = c.ipc.catalog(None, cat).await?;
    let rows = list.as_array().ok_or_else(Error::protocol)?;
    let page = q["page_size"].as_u64().unwrap_or(50) as usize;
    let mut registry = c.cursors.lock().unwrap();
    let now = Instant::now();
    registry.retain(|_, p| p.expires > now);
    let (ids, offset) = if let Some(t) = q["page_token"].as_str() {
        let p = registry
            .get(t)
            .filter(|p| p.category.as_deref() == cat)
            .ok_or_else(|| Error::new(400, "bad_request", "Invalid or expired page token."))?;
        (p.ids.clone(), p.offset)
    } else {
        (
            rows.iter()
                .filter_map(|r| r["id"].as_str().map(str::to_owned))
                .collect(),
            0,
        )
    };
    let items: HashMap<_, _> = rows
        .iter()
        .filter_map(|r| r["id"].as_str().map(|id| (id, r)))
        .collect();
    let mut batch = Vec::new();
    let mut pos = offset;
    while pos < ids.len() && batch.len() < page {
        if let Some(r) = items.get(ids[pos].as_str()) {
            batch.push(*r);
        }
        pos += 1;
    }
    let next = if pos < ids.len() {
        if registry.len() >= 10000
            && let Some(old) = registry
                .iter()
                .min_by_key(|(_, p)| p.expires)
                .map(|(k, _)| k.clone())
        {
            registry.remove(&old);
        }
        let token = Uuid::new_v4().to_string();
        registry.insert(
            token.clone(),
            Cursor {
                category: cat.map(str::to_owned),
                ids,
                offset: pos,
                expires: now + std::time::Duration::from_secs(86400),
            },
        );
        Some(token)
    } else {
        None
    };
    json_response(200, json!({"items":batch,"next_page_token":next}))
}
