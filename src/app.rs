use crate::config::Options;
use crate::config::Shared;
use crate::{
    auth::owner,
    catalog::list_blueprints,
    config::Config,
    error::problem,
    http::{json_response, origin, response, send},
    mcp, oauth, schema, ui,
    uploads::{too_large, upload_body},
};
use axum::{
    Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    http::{HeaderValue, header},
    response::{IntoResponse, Response},
};
use clap::Parser;
use mn_client_support::{Error, Result, connection, control::Method, resources};
use serde_json::{Value, json};
use std::{collections::HashMap, sync::Arc};
use uuid::Uuid;
pub async fn run() -> Result<()> {
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
    let mut config = Config::new(home, token);
    config.limit = o.request_limit;
    config.upload = o.upload_limit;
    let app = router(
        config
            .with_cors_origins(&std::env::var("MN_API_CORS_ALLOW_ORIGINS").unwrap_or_default())?,
    );
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
/// Build the same router used by the installed HTTP server.
pub fn router(config: Config) -> Router {
    Router::new()
        .fallback(dispatch)
        .with_state(Arc::new(config))
}
async fn dispatch(State(c): State<Shared>, r: Request) -> Response {
    let p = r.uri().path().to_owned();
    let limit = match (r.method().as_str(), p.trim_end_matches('/')) {
        ("POST", "/api/v1/bundles") => c.upload + 1024 * 1024,
        ("POST", "/api/v1/job-restorations") => 128 * 1024 * 1024 * 1024,
        _ => c.limit,
    };
    if let Some(length) = r.headers().get("content-length") {
        match length.to_str().ok().and_then(|s| s.parse::<i128>().ok()) {
            None => {
                return problem(
                    Error::new(
                        400,
                        "invalid_content_length",
                        "Content-Length must be an integer.",
                    ),
                    &p,
                    "",
                );
            }
            Some(n) if n > limit as i128 => {
                return problem(
                    Error::new(
                        413,
                        "request_too_large",
                        &format!("The request exceeds the {limit} byte limit."),
                    ),
                    &p,
                    "",
                );
            }
            _ => {}
        }
    }
    let id = r
        .headers()
        .get("x-request-id")
        .filter(|v| !v.is_empty() && v.len() <= 255)
        .cloned()
        .unwrap_or_else(|| Uuid::new_v4().to_string().parse().unwrap());
    let origin = r.headers().get("origin").cloned();
    let mut response = if let (Some(cors), Some(origin)) = (&c.cors, &origin) {
        if r.method() == "OPTIONS" && r.headers().contains_key("access-control-request-method") {
            cors.preflight(origin, r.headers())
        } else {
            let mut response = route(&c, r, &p)
                .await
                .unwrap_or_else(|e| problem(e, &p, id.to_str().unwrap_or_default()));
            cors.simple(origin, response.headers_mut());
            response
        }
    } else {
        route(&c, r, &p)
            .await
            .unwrap_or_else(|e| problem(e, &p, id.to_str().unwrap_or_default()))
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
    if matches!(m.as_str(), "GET" | "HEAD") {
        let doc = match path {
            "/docs" => Some(include_str!("docs/docs.html")),
            "/redoc" => Some(include_str!("docs/redoc.html")),
            "/docs/oauth2-redirect" => Some(include_str!("docs/oauth2-redirect.html")),
            _ => None,
        };
        if let Some(html) = doc {
            return Ok(axum::response::Html(html).into_response());
        }
    }
    if path.starts_with("/.well-known/") || path.starts_with("/api/v1/mcp/oauth/") {
        return oauth::route(c, r, path).await;
    }
    let path = path
        .strip_prefix("/api/v1/")
        .map(|p| format!("/{p}"))
        .ok_or_else(|| Error::new(404, "not_found", "Not Found"))?;
    if m == "GET" && path == "/health" {
        return json_response(
            200,
            json!({"status":"ok","api_contract":"mirrorneuron.rest.v1","auth":if c.token.is_empty(){"disabled"}else{"enabled"}}),
        );
    }
    if matches!(m.as_str(), "GET" | "HEAD") && path == "/openapi.json" {
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
    let custom = matches!(segments.as_slice(),["blueprints"] if m=="GET")
        || matches!(segments.as_slice(), ["jobs",_, "mcp-access"] if matches!(m.as_str(),"GET"|"PUT"))
        || matches!(segments.as_slice(), ["jobs",_,"mcp-sessions"] if m=="POST")
        || matches!(segments.as_slice(), ["jobs", _, "ui-proxy", _, ..]);
    let resolved = resources::resolve(&m, &path);
    if !custom && let Err(e) = &resolved {
        if let ["jobs", job, _, ..] = segments.as_slice() {
            return mcp::protocol(c, r, job).await;
        }
        if let Some(trimmed) = path.strip_suffix('/')
            && resources::resolve(&m, trimmed).is_ok()
        {
            let location = format!(
                "{}/api/v1{}{}",
                origin(&r),
                trimmed,
                r.uri().query().map(|q| format!("?{q}")).unwrap_or_default()
            );
            return send(307, Body::empty(), [("location".into(), location)]);
        }
        if e.status == 405 || path == "/openapi.json" || path == "/blueprints" {
            let allow = ["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"]
                .into_iter()
                .find(|method| resources::resolve(method, &path).is_ok())
                .unwrap_or("GET");
            let mut error = Error::new(405, "method_not_allowed", "Method Not Allowed");
            error.headers.insert("allow".into(), allow.into());
            return Err(error);
        }
        return Err(Error::new(404, "not_found", "Not Found"));
    }
    if !c.token.is_empty() && !owner(c, r.headers()) {
        return Err(Error::new(
            401,
            "authentication_required",
            "missing or invalid bearer [redacted]",
        ));
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
    let (ep, mut variables) = resolved?;
    let op = schema::operation(&ep.path, &m);
    let params = schema::parameters(op, &mut variables, &mut q, &headers);
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
    let context = if action == "get_job_ui" || ep.parameters.values().any(|p| p.source == "context")
    {
        json!({"headers":context_headers,"method":m,"path":format!("/api/v1{path}"),"query":raw_query})
    } else {
        json!({})
    };
    let (body, chunks, filename) = if upload {
        params?;
        let (chunks, name) = upload_body(action, r, limit, c.upload).await?;
        (None, Some(chunks), name)
    } else {
        let bytes = to_bytes(r.into_body(), limit)
            .await
            .map_err(|_| too_large())?;
        let mut body = if bytes.is_empty()
            || op
                .pointer("/requestBody/content/application~1json")
                .is_none()
        {
            None
        } else {
            Some(serde_json::from_slice(&bytes).map_err(|e| schema::malformed(&bytes, e))?)
        };
        schema::validate_request(params, schema::body(op, body.as_mut()))?;
        (body, None, None)
    };
    let mut args = schema::arguments(
        ep,
        op,
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
