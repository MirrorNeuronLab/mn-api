use crate::{
    config::Config,
    grants::Store,
    http::{json_response, origin},
    schema,
    uploads::too_large,
};
use axum::{body::to_bytes, extract::Request, response::Response};
use mn_client_support::{Error, Result, control::Method, resources};
use serde_json::{Value, json};
use url::Url;
pub async fn admin(c: &Config, r: Request, job: &str, kind: &str) -> Result<Response> {
    let base = origin(&r);
    let method = r.method().as_str().to_owned();
    let job = &resources::decode_id(job)?;
    let value = if method == "PUT" {
        let raw = to_bytes(r.into_body(), c.limit)
            .await
            .map_err(|_| too_large())?;
        let mut b = if raw.is_empty() {
            None
        } else {
            Some(serde_json::from_slice(&raw).map_err(|e| schema::malformed(&raw, e))?)
        };
        schema::body(
            schema::operation("/jobs/{job_id}/mcp-access", "PUT"),
            b.as_mut(),
        )?;
        b.unwrap_or_default()
    } else {
        Value::Null
    };
    let record = c
        .ipc
        .call(
            Method::Read,
            "core.get_job",
            json!({"args":[job],"kwargs":{}}),
            "",
            30,
        )
        .await?
        .checked()?;
    let record = if let Some(s) = record.as_str() {
        serde_json::from_str(s).map_err(|_| Error::protocol())?
    } else {
        record
    };
    if record["deleted"] == true
        || record["deleted_at"].is_string()
        || (method != "GET" && !record["mcp"].is_object())
    {
        return Err(Error::new(
            404,
            "not_found",
            "Co-worker MCP is unavailable.",
        ));
    }
    let mut receipt = None;
    match (method.as_str(), kind) {
        ("POST", "mcp-sessions") => {
            return json_response(
                200,
                json!({"access_token":Store::open(&c.home)?.issue("internal",Some(job),0,86400,&json!({}))?,"expires_in":86400}),
            );
        }
        ("GET", "mcp-access") => {}
        ("PUT", "mcp-access") => {
            let b = &value;
            let enabled = b["enabled"]
                .as_bool()
                .ok_or_else(|| Error::invalid("Invalid enabled flag."))?;
            if enabled && c.token.is_empty() {
                return Err(Error::new(
                    409,
                    "conflict",
                    "Configure MN_API_TOKEN before enabling external MCP connections.",
                ));
            }
            if enabled && record["status"] == "archived" {
                return Err(Error::new(
                    409,
                    "conflict",
                    "Restore the co-worker before allowing new connections.",
                ));
            }
            let raw = b["server_url"].as_str().unwrap_or_default();
            let valid = Url::parse(raw).is_ok_and(|u| {
                u.host().is_some()
                    && u.username().is_empty()
                    && u.password().is_none()
                    && u.query().is_none()
                    && u.fragment().is_none()
                    && u.path() == format!("/api/v1/jobs/{}/mcp", resources::resource_id(job))
                    && (u.scheme() == "https"
                        || u.scheme() == "http"
                            && matches!(u.host_str(), Some("127.0.0.1" | "localhost" | "[::1]")))
            });
            if raw.chars().any(|ch| ch <= ' ') {
                return Err(Error::new(
                    422,
                    "validation_failed",
                    "Use an MCP URL without whitespace or control characters.",
                ));
            }
            if !valid {
                return Err(Error::new(
                    422,
                    "validation_failed",
                    "Use an HTTPS MCP URL or a local loopback URL for this Job.",
                ));
            }
            receipt = Some(json!(Store::open(&c.home)?.configure(job, enabled, raw)?));
        }
        _ => return Err(Error::new(405, "method_not_allowed", "Unsupported method.")),
    }
    let p = Store::open(&c.home)?.policy(job)?;
    let mut v = json!({"job_id":job,"enabled":p["enabled"],"server_url":p["server_url"].as_str().filter(|s|!s.is_empty()).map(str::to_owned).unwrap_or_else(||format!("{base}/api/v1/jobs/{}/mcp",resources::resource_id(job))),"authentication":"oauth2","skills_extension":"io.modelcontextprotocol/skills"});
    if let Some(code) = receipt {
        v["connection_code"] = code;
    }
    json_response(200, v)
}
