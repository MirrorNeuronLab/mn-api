//! Serve the built browser client and relay its API requests to the native API.
use axum::{
    Json, Router,
    body::Body,
    extract::{
        FromRequestParts, Request, State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use std::{
    env,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use subtle::ConstantTimeEq;
use tokio_tungstenite::{
    connect_async,
    tungstenite::{Message as Wire, client::IntoClientRequest},
};
use tower_http::services::ServeFile;
use url::Url;

#[derive(Parser)]
#[command(version, about = "Serve the MirrorNeuron web UI")]
struct Options {
    #[arg(long, env = "MN_WEB_UI_HOST", default_value = "localhost")]
    host: String,
    #[arg(long, env = "MN_WEB_UI_PORT", default_value_t = 55173)]
    port: u16,
    #[arg(long, env = "MN_WEB_UI_API_BASE_URL")]
    api_base_url: Option<String>,
    #[arg(long, env = "MN_WEB_UI_DIST_DIR")]
    dist_dir: Option<PathBuf>,
    #[arg(long, env = "MN_WEB_UI_PROXY_TIMEOUT_SECONDS", default_value_t = 30.0)]
    timeout: f64,
}
struct Site {
    dist: PathBuf,
    api: String,
    credential: Option<HeaderValue>,
    http: reqwest::Client,
    timeout: Duration,
}
type Shared = Arc<Site>;
type Failure = (StatusCode, Json<serde_json::Value>);
fn failure(status: StatusCode, detail: &str) -> Failure {
    (status, Json(json!({"detail":detail})))
}
fn upstream() -> Failure {
    failure(
        StatusCode::BAD_GATEWAY,
        "The MirrorNeuron API could not be reached.",
    )
}

pub async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let o = Options::parse();
    let cwd = env::current_dir()?;
    let dist = o.dist_dir.unwrap_or_else(|| {
        if cwd.join("dist/index.html").is_file() {
            cwd.join("dist")
        } else {
            cwd
        }
    });
    let root = mn_client_support::connection::home()?;
    let token = env::var("MN_API_TOKEN")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::fs::read_to_string(root.join("api.token")).ok());
    let credential = token
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| HeaderValue::from_str(&format!("Bearer {s}")))
        .transpose()?;
    let api = o
        .api_base_url
        .or_else(|| env::var("MN_API_BASE_URL").ok())
        .unwrap_or_else(|| {
            format!(
                "http://{}:{}/api/v1",
                env::var("MN_API_HOST").unwrap_or_else(|_| "localhost".into()),
                env::var("MN_API_PORT").unwrap_or_else(|_| "54001".into())
            )
        });
    let api = Url::parse(&api)?;
    if !matches!(api.scheme(), "http" | "https")
        || api.host().is_none()
        || !api.username().is_empty()
        || api.password().is_some()
        || api.query().is_some()
        || api.fragment().is_some()
    {
        return Err("Invalid API base URL".into());
    }
    let timeout = Duration::try_from_secs_f64(o.timeout)?;
    let http = reqwest::Client::builder()
        .connect_timeout(timeout)
        .read_timeout(timeout)
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let site = Arc::new(Site {
        dist: std::fs::canonicalize(&dist).unwrap_or(dist),
        api: api.as_str().trim_end_matches('/').into(),
        credential,
        http,
        timeout,
    });
    let listener = tokio::net::TcpListener::bind((o.host.as_str(), o.port)).await?;
    axum::serve(listener, Router::new().fallback(serve).with_state(site)).await?;
    Ok(())
}

mod proxy;
mod websocket;
use proxy::{filter, proxy, query, service_headers};
use websocket::websocket;
async fn serve(State(site): State<Shared>, r: Request) -> Result<Response, Failure> {
    let path = r.uri().path().to_owned();
    if path == "/health" && r.method() == Method::GET {
        return Ok(Json(json!({"component":"web-ui","status":if site.dist.join("index.html").is_file(){"ok"}else{"missing"},"api_base_url":site.api,"dist_dir":site.dist})).into_response());
    }
    if let Some(path) = path.strip_prefix("/api/") {
        let path = if site.api.ends_with("/v1") {
            path.strip_prefix("v1/").unwrap_or(path)
        } else {
            path
        };
        return proxy(&site, r, &format!("{}/{path}", site.api), None).await;
    }
    if let Some(rel) = path.strip_prefix("/job-ui-proxy/") {
        let mut parts = rel.splitn(3, '/');
        let id = parts.next().unwrap_or_default();
        let port = parts
            .next()
            .and_then(|p| p.parse::<u16>().ok())
            .filter(|p| *p > 0)
            .ok_or_else(|| failure(StatusCode::BAD_REQUEST, "Invalid job UI port."))?;
        let tail = parts.next().unwrap_or_default();
        if id.is_empty() || !matches!(*r.method(), Method::GET | Method::HEAD) {
            return Err(failure(StatusCode::BAD_REQUEST, "Invalid job UI request."));
        }
        let ws = r
            .headers()
            .get(header::UPGRADE)
            .is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"websocket"));
        let probe = r
            .headers()
            .get("x-mn-job-ui-probe")
            .is_some_and(|v| v == "1")
            && site.credential.as_ref().is_none_or(|c| {
                r.headers()
                    .get(header::AUTHORIZATION)
                    .is_some_and(|h| bool::from(c.as_bytes().ct_eq(h.as_bytes())))
            });
        let mut headers = filter(r.headers(), true, false);
        service_headers(&site, &mut headers);
        let handle = format!(
            "{}/jobs/{id}/ui{}",
            site.api,
            if probe { "?check_readiness=false" } else { "" }
        );
        let mut auth = HeaderMap::new();
        service_headers(&site, &mut auth);
        let handle = site
            .http
            .get(handle)
            .headers(auth)
            .send()
            .await
            .map_err(|_| upstream())?;
        if !handle.status().is_success() {
            return Err(failure(
                handle.status(),
                "No Web UI is registered for this job.",
            ));
        }
        let value: serde_json::Value =
            serde_json::from_slice(&handle.bytes().await.map_err(|_| upstream())?)
                .map_err(|_| upstream())?;
        let execution = value
            .pointer("/web_ui/transport/execution_id")
            .and_then(|v| v.as_str())
            .and_then(|s| HeaderValue::from_str(s).ok())
            .ok_or_else(|| failure(StatusCode::BAD_GATEWAY, "Invalid job UI transport."))?;
        headers.insert("x-mn-ui-execution", execution);
        headers.insert("x-mn-ui-hop", HeaderValue::from_static("1"));
        if probe {
            headers.insert("x-mn-job-ui-probe", HeaderValue::from_static("1"));
        }
        let tail = if ws && tail == "ws" { "ws/" } else { tail };
        let target = format!("{}/jobs/{id}/ui-proxy/{port}/{tail}", site.api);
        if ws {
            return websocket(&site, r, &target, headers).await;
        }
        return proxy(&site, r, &target, Some((id, headers))).await;
    }
    if !matches!(*r.method(), Method::GET | Method::HEAD) {
        return Err(failure(
            StatusCode::METHOD_NOT_ALLOWED,
            "Unsupported request method.",
        ));
    }
    let index = site.dist.join("index.html");
    if !index.is_file() {
        return Err(failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "The Web UI build is missing.",
        ));
    }
    let relative = mn_client_support::resources::decode_id(path.trim_start_matches('/'))
        .map_err(|_| failure(StatusCode::BAD_REQUEST, "Invalid asset path."))?;
    if Path::new(&relative).components().any(|c| {
        !matches!(
            c,
            std::path::Component::Normal(_) | std::path::Component::CurDir
        )
    }) {
        return Err(failure(StatusCode::NOT_FOUND, "Unknown asset."));
    }
    let asset = match tokio::fs::canonicalize(site.dist.join(relative)).await {
        Ok(p) if !p.starts_with(&site.dist) => {
            return Err(failure(StatusCode::NOT_FOUND, "Unknown asset."));
        }
        Ok(p) if p.is_file() => p,
        _ => index,
    };
    Ok(ServeFile::new(asset)
        .try_call(r)
        .await
        .map_err(|_| failure(StatusCode::INTERNAL_SERVER_ERROR, "Could not read asset."))?
        .map(Body::new))
}
