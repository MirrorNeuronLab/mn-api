use super::*;
pub(super) async fn websocket(
    site: &Site,
    r: Request,
    target: &str,
    headers: HeaderMap,
) -> Result<Response, Failure> {
    let mut u = Url::parse(&query(target, r.uri().query())).map_err(|_| upstream())?;
    u.set_scheme(if u.scheme() == "https" { "wss" } else { "ws" })
        .map_err(|_| upstream())?;
    let mut request = u.as_str().into_client_request().map_err(|_| upstream())?;
    for name in [
        "authorization",
        "x-mn-ui-execution",
        "x-mn-ui-hop",
        "x-mn-job-ui-probe",
    ] {
        if let Some(v) = headers.get(name) {
            request.headers_mut().insert(name, v.clone());
        }
    }
    if let Some(v) = r.headers().get(header::SEC_WEBSOCKET_PROTOCOL) {
        request
            .headers_mut()
            .insert(header::SEC_WEBSOCKET_PROTOCOL, v.clone());
    }
    let (mut parts, _) = r.into_parts();
    let upgrade = WebSocketUpgrade::from_request_parts(&mut parts, &())
        .await
        .map_err(|_| failure(StatusCode::BAD_REQUEST, "Invalid WebSocket request."))?;
    let (wire, reply) = tokio::time::timeout(site.timeout, connect_async(request))
        .await
        .map_err(|_| upstream())?
        .map_err(|_| upstream())?;
    let upgrade = if let Some(v) = reply
        .headers()
        .get(header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|h| h.to_str().ok())
    {
        upgrade.protocols([v.to_owned()])
    } else {
        upgrade
    };
    Ok(upgrade.on_upgrade(move |browser| bridge(browser, wire)))
}
async fn bridge(
    browser: WebSocket,
    wire: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
) {
    let (mut tx, mut rx) = wire.split();
    let (mut browser_tx, mut browser_rx) = browser.split();
    let outgoing = async {
        while let Some(Ok(message)) = browser_rx.next().await {
            let message =
                match message {
                    Message::Text(s) => Wire::Text(s.as_str().into()),
                    Message::Binary(b) => Wire::Binary(b),
                    Message::Close(c) => Wire::Close(c.map(|f| {
                        tokio_tungstenite::tungstenite::protocol::CloseFrame {
                            code: f.code.into(),
                            reason: f.reason.as_str().into(),
                        }
                    })),
                    _ => continue,
                };
            if tx.send(message).await.is_err() {
                break;
            }
        }
    };
    let incoming = async {
        while let Some(Ok(message)) = rx.next().await {
            let message = match message {
                Wire::Text(s) => Message::Text(s.as_str().into()),
                Wire::Binary(b) => Message::Binary(b),
                Wire::Close(c) => Message::Close(c.map(|f| axum::extract::ws::CloseFrame {
                    code: f.code.into(),
                    reason: f.reason.as_str().into(),
                })),
                _ => continue,
            };
            if browser_tx.send(message).await.is_err() {
                break;
            }
        }
    };
    tokio::select! {_=outgoing=>{},_=incoming=>{}}
}
