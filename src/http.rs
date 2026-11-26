pub(crate) mod cors;
use crate::auth::header_str;
use axum::{
    body::Body,
    extract::Request,
    http::{HeaderName, HeaderValue, StatusCode},
    response::Response,
};
use mn_client_support::{Error, Result, control::Reply};
use serde_json::{Value, json};
pub(crate) fn origin(r: &Request) -> String {
    format!("http://{}", header_str(r.headers(), "host"))
}
pub(crate) fn query(s: &str) -> Value {
    Value::Object(
        url::form_urlencoded::parse(s.as_bytes())
            .map(|(k, v)| (k.into_owned(), json!(v)))
            .collect(),
    )
}
pub(crate) fn send(
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
pub(crate) fn json_response(status: u16, v: Value) -> Result<Response> {
    send(
        status,
        Body::from(v.to_string()),
        [("content-type".into(), "application/json".into())],
    )
}
pub(crate) fn response(r: Reply) -> Result<Response> {
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
