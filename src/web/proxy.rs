use super::*;
pub(super) fn filter(headers: &HeaderMap, job: bool, response: bool) -> HeaderMap {
    let mut kept = HeaderMap::new();
    let connection: Vec<_> = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|s| s.split(',').map(str::trim))
        .collect();
    for (k, v) in headers {
        let name = k.as_str();
        if matches!(
            name,
            "host"
                | "connection"
                | "keep-alive"
                | "proxy-authenticate"
                | "proxy-authorization"
                | "proxy-connection"
                | "transfer-encoding"
                | "upgrade"
                | "trailer"
                | "te"
                | "content-length"
        ) || connection.iter().any(|n| name.eq_ignore_ascii_case(n))
        {
            continue;
        }
        if job
            && (matches!(
                name,
                "authorization"
                    | "cookie"
                    | "set-cookie"
                    | "location"
                    | "x-mn-ui-execution"
                    | "x-mn-ui-hop"
                    | "x-mn-job-ui-probe"
            ) || name.starts_with("sec-websocket-"))
        {
            continue;
        }
        if response && matches!(name, "date" | "server") {
            continue;
        }
        kept.append(k, v.clone());
    }
    kept
}
pub(super) fn service_headers(site: &Site, headers: &mut HeaderMap) {
    if !headers.contains_key(header::AUTHORIZATION)
        && let Some(token) = &site.credential
    {
        headers.insert(header::AUTHORIZATION, token.clone());
    }
}
pub(super) async fn proxy(
    site: &Site,
    r: Request,
    target: &str,
    job: Option<(&str, HeaderMap)>,
) -> Result<Response, Failure> {
    let target = query(target, r.uri().query());
    let method = r.method().clone();
    let id = job.as_ref().map(|(id, _)| *id);
    let mut headers = job
        .map(|(_, headers)| headers)
        .unwrap_or_else(|| filter(r.headers(), false, false));
    service_headers(site, &mut headers);
    let upstream = site
        .http
        .request(method.clone(), target)
        .headers(headers)
        .body(reqwest::Body::wrap_stream(r.into_body().into_data_stream()))
        .send()
        .await
        .map_err(|_| upstream())?;
    let status = upstream.status();
    let mut headers = filter(upstream.headers(), id.is_some(), true);
    let html = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|s| s.starts_with("text/html"));
    let body = if let Some(id) = id
        && method == Method::GET
        && status == StatusCode::OK
        && html
        && !headers.contains_key(header::CONTENT_ENCODING)
    {
        let mut body = upstream
            .bytes()
            .await
            .map_err(|_| failure(StatusCode::BAD_GATEWAY, "Incomplete job UI response."))?
            .to_vec();
        let base = json!(format!("/job-ui-proxy/{id}"))
            .to_string()
            .replace('<', "\\u003c")
            .replace('>', "\\u003e");
        let script = format!("<script>window.__MN_JOB_UI_PROXY_BASE__={base};</script>");
        let pos = body
            .windows(7)
            .rposition(|b| b.eq_ignore_ascii_case(b"</head>"))
            .unwrap_or(0);
        body.splice(pos..pos, script.bytes());
        headers.remove(header::ETAG);
        headers.remove(header::LAST_MODIFIED);
        Body::from(body)
    } else {
        Body::from_stream(upstream.bytes_stream())
    };
    let mut out = Response::new(body);
    *out.status_mut() = status;
    *out.headers_mut() = headers;
    Ok(out)
}
pub(super) fn query(target: &str, q: Option<&str>) -> String {
    q.map_or_else(|| target.to_owned(), |q| format!("{target}?{q}"))
}
