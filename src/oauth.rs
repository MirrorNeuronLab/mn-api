use super::*;
use crate::grants::{Store, digest};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use sha2::{Digest, Sha256};
use url::Url;

const ROOT: &str = "/api/v1/mcp/oauth";
fn field<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap_or_default()
}
fn bad() -> Error {
    Error::new(400, "invalid_request", "Invalid OAuth request.")
}
fn rejection(code: &str) -> Result<Response> {
    json_response(400, json!({"error":code}))
}
fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}
fn issuer(resource: &str) -> Result<String> {
    let u = Url::parse(resource).map_err(|_| bad())?;
    Ok(format!("{}{ROOT}", u.origin().ascii_serialization()))
}
fn safe_callback(v: &str) -> bool {
    Url::parse(v).is_ok_and(|u| {
        u.fragment().is_none()
            && u.username().is_empty()
            && u.password().is_none()
            && match u.scheme() {
                "https" => u.host().is_some(),
                "http" => matches!(u.host_str(), Some("127.0.0.1" | "[::1]" | "localhost")),
                "javascript" | "data" | "file" => false,
                scheme => scheme.contains('.') || scheme == "vscode" || scheme == "cursor",
            }
    })
}
fn redirect(url: &str) -> Result<Response> {
    send(
        303,
        Body::empty(),
        [
            ("location".into(), url.into()),
            ("cache-control".into(), "no-store".into()),
        ],
    )
}
fn scopes(s: &str) -> Option<Vec<&str>> {
    let v: Vec<_> = s.split_whitespace().collect();
    (v.contains(&"co_worker:supervise")
        && v.iter()
            .all(|s| matches!(*s, "co_worker:supervise" | "offline_access")))
    .then_some(v)
}
pub async fn route(c: &Config, r: Request, path: &str) -> Result<Response> {
    let base = origin(&r);
    let method = r.method().as_str().to_owned();
    let mut q = query(r.uri().query().unwrap_or_default());
    if method == "POST" {
        let raw = to_bytes(r.into_body(), c.limit.min(65536))
            .await
            .map_err(|_| too_large())?;
        q = if path.ends_with("/register") {
            serde_json::from_slice(&raw).map_err(|_| bad())?
        } else {
            query(std::str::from_utf8(&raw).map_err(|_| bad())?)
        };
    }
    let db = Store::open(&c.home)?;
    if let Some(job) = path
        .strip_prefix("/.well-known/oauth-protected-resource/api/v1/jobs/")
        .and_then(|p| p.strip_suffix("/mcp"))
        && method == "GET"
    {
        let p = db.policy(job)?;
        if p["enabled"] != true {
            return Err(Error::new(404, "not_found", "External MCP is disabled."));
        }
        return json_response(
            200,
            json!({"resource":p["server_url"],"authorization_servers":[issuer(field(&p,"server_url"))?],"scopes_supported":["co_worker:supervise","offline_access"],"bearer_methods_supported":["header"]}),
        );
    }
    if path == "/.well-known/oauth-authorization-server/api/v1/mcp/oauth" && method == "GET" {
        let i = format!("{base}{ROOT}");
        return json_response(
            200,
            json!({"issuer":i,"authorization_endpoint":format!("{i}/authorize"),"token_endpoint":format!("{i}/token"),"registration_endpoint":format!("{i}/register"),"revocation_endpoint":format!("{i}/revoke"),"token_endpoint_auth_methods_supported":["none"],"scopes_supported":["co_worker:supervise","offline_access"],"response_types_supported":["code"],"grant_types_supported":["authorization_code","refresh_token"],"code_challenge_methods_supported":["S256"]}),
        );
    }
    match (method.as_str(), path.strip_prefix(ROOT).unwrap_or_default()) {
        ("POST", "/register") => {
            if q.get("token_endpoint_auth_method")
                .is_some_and(|v| v != "none")
                || !q["redirect_uris"].as_array().is_some_and(|a| {
                    !a.is_empty() && a.iter().all(|v| v.as_str().is_some_and(safe_callback))
                })
            {
                return rejection("invalid_client_metadata");
            }
            q.as_object_mut().ok_or_else(bad)?.retain(|k, _| {
                matches!(
                    k.as_str(),
                    "client_name"
                        | "redirect_uris"
                        | "scope"
                        | "grant_types"
                        | "response_types"
                        | "token_endpoint_auth_method"
                )
            });
            q["token_endpoint_auth_method"] = "none".into();
            let id = db.issue("client", None, 0, 86400 * 3650, &q)?;
            q["client_id"] = id.into();
            json_response(201, q)
        }
        ("GET", "/authorize") => {
            let Some(client) = db.grant(field(&q, "client_id"), Some("client"), false)? else {
                return rejection("invalid_client");
            };
            let resource = db.resource(field(&q, "resource"))?;
            if resource.is_null() {
                return rejection("invalid_target");
            }
            let challenge = field(&q, "code_challenge");
            if q["response_type"] != "code"
                || q["code_challenge_method"] != "S256"
                || challenge.len() != 43
                || !challenge
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
                || !client["data"]["redirect_uris"]
                    .as_array()
                    .is_some_and(|a| a.contains(&q["redirect_uri"]))
            {
                return rejection("invalid_request");
            }
            if scopes(field(&q, "scope")).is_none() {
                return rejection("invalid_scope");
            }
            let ticket = db.issue(
                "consent",
                resource["job_id"].as_str(),
                resource["generation"].as_i64().ok_or_else(bad)?,
                600,
                &json!({"params":q,"client_name":client["data"]["client_name"]}),
            )?;
            redirect(&format!(
                "{}/approve?ticket={ticket}",
                issuer(field(&q, "resource"))?
            ))
        }
        ("GET" | "POST", "/approve") => {
            let ticket = field(&q, "ticket");
            let g = db.grant(ticket, Some("consent"), false)?.ok_or_else(bad)?;
            let job = field(&g, "job_id");
            let policy = db.policy(job)?;
            let params = &g["data"]["params"];
            if method == "POST"
                && policy["key_hash"]
                    .as_str()
                    .is_some_and(|h| equal(&digest(field(&q, "connection_code")), h))
            {
                db.grant(ticket, Some("consent"), true)?.ok_or_else(bad)?;
                let code = db.issue(
                    "code",
                    Some(job),
                    g["generation"].as_i64().ok_or_else(bad)?,
                    300,
                    params,
                )?;
                let mut target = Url::parse(field(params, "redirect_uri")).map_err(|_| bad())?;
                {
                    let mut pairs = target.query_pairs_mut();
                    pairs.append_pair("code", &code);
                    if let Some(state) = params["state"].as_str() {
                        pairs.append_pair("state", state);
                    }
                }
                return redirect(target.as_str());
            }
            let name = escape(g["data"]["client_name"].as_str().unwrap_or("AI client"));
            let job = escape(job);
            let mut target = Url::parse(field(params, "redirect_uri")).map_err(|_| bad())?;
            target.set_query(None);
            target.set_fragment(None);
            let allowed = if target.host().is_some() {
                target.origin().ascii_serialization()
            } else {
                format!("{}:", target.scheme())
            };
            let page = format!(
                r#"<!doctype html><html lang="en"><meta charset="utf-8"><title>Connect co-worker</title><h1>Connect {name}</h1><p>Allow this client to read work and control runs for {job}.</p><form method="post"><input type="hidden" name="ticket" value="{}"><label>Connection code <input type="password" name="connection_code" required maxlength="256"></label><button>Allow connection</button></form></html>"#,
                escape(ticket)
            );
            send(
                if method == "POST" { 403 } else { 200 },
                Body::from(page),
                [
                    ("content-type".into(), "text/html; charset=utf-8".into()),
                    (
                        "content-security-policy".into(),
                        format!(
                            "default-src 'none'; form-action 'self' {allowed}; frame-ancestors 'none'"
                        ),
                    ),
                    ("cache-control".into(), "no-store".into()),
                    ("referrer-policy".into(), "no-referrer".into()),
                ],
            )
        }
        ("POST", "/token") => {
            let (kind, key) = match field(&q, "grant_type") {
                "authorization_code" => ("code", field(&q, "code")),
                "refresh_token" => ("refresh", field(&q, "refresh_token")),
                _ => return rejection("unsupported_grant_type"),
            };
            let Some(g) = db.grant(key, Some(kind), false)? else {
                return rejection("invalid_grant");
            };
            let d = &g["data"];
            if q["client_id"] != d["client_id"]
                || q.get("resource").is_some_and(|v| v != &d["resource"])
            {
                return rejection("invalid_grant");
            }
            if kind == "code" {
                let verifier = field(&q, "code_verifier");
                if !(43..=128).contains(&verifier.len())
                    || !verifier
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"-._~".contains(&b))
                    || q["redirect_uri"] != d["redirect_uri"]
                    || !equal(
                        &URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes())),
                        field(d, "code_challenge"),
                    )
                {
                    return rejection("invalid_grant");
                }
            }
            let prior = if let Some(v) = d["scopes"].as_array() {
                v.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(" ")
            } else {
                field(d, "scope").into()
            };
            let scope = q["scope"].as_str().unwrap_or(&prior);
            let Some(scopes) = scopes(scope) else {
                return rejection("invalid_scope");
            };
            if !scopes
                .iter()
                .all(|s| prior.split_whitespace().any(|p| p == *s))
            {
                return rejection("invalid_scope");
            }
            if db.grant(key, Some(kind), true)?.is_none() {
                return rejection("invalid_grant");
            }
            let data = json!({"client_id":q["client_id"],"resource":d["resource"],"scopes":scopes,"subject":g["job_id"]});
            let token = |k, ttl| {
                db.issue(
                    k,
                    g["job_id"].as_str(),
                    g["generation"].as_i64().ok_or_else(bad)?,
                    ttl,
                    &data,
                )
            };
            let mut res = json_response(
                200,
                json!({"token_type":"Bearer","access_token":token("access",3600)?,"expires_in":3600,"refresh_token":token("refresh",30*86400)?,"scope":scope}),
            )?;
            res.headers_mut()
                .insert("cache-control", HeaderValue::from_static("no-store"));
            Ok(res)
        }
        ("POST", "/revoke") => {
            let token = field(&q, "token");
            if let Some(g) = db.grant(token, None, false)?
                && g["data"]["client_id"] == q["client_id"]
            {
                db.revoke(token)?;
            }
            send(200, Body::empty(), [])
        }
        _ => Err(Error::new(404, "not_found", "Resource not found.")),
    }
}
