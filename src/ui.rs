use super::*;
use axum::extract::{
    FromRequestParts,
    ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
};
use futures_util::SinkExt;
use mn_client_support::{
    adapter::{Frame, FrameMetadata, UiFrames},
    control::{Bytes, FRAME_BYTES},
};
use tokio_stream::wrappers::ReceiverStream;
const MESSAGE_LIMIT: usize = 16 * 1024 * 1024;
fn frame(kind: &str, bytes: Bytes) -> Frame {
    Frame {
        kind: kind.into(),
        data: bytes,
        metadata: None,
    }
}
fn close(code: u16, reason: &str) -> Frame {
    Frame {
        kind: "close".into(),
        data: Bytes::new(),
        metadata: Some(FrameMetadata {
            status: 0,
            value_json: None,
            headers: vec![
                ("code".into(), code.to_string()),
                ("reason".into(), reason.into()),
            ],
        }),
    }
}
pub async fn relay(c: &Config, r: Request, job: &str, port: &str, path: &str) -> Result<Response> {
    let port = port
        .parse::<u16>()
        .ok()
        .filter(|n| *n > 0)
        .ok_or_else(|| Error::invalid("Invalid job UI port."))?;
    let ws = header_str(r.headers(), "upgrade").eq_ignore_ascii_case("websocket");
    let context: HashMap<_, _> = r
        .headers()
        .iter()
        .filter(|(k, _)| {
            !matches!(
                k.as_str(),
                "authorization"
                    | "proxy-authorization"
                    | "host"
                    | "cookie"
                    | "connection"
                    | "upgrade"
                    | "transfer-encoding"
            ) && !k.as_str().starts_with("sec-websocket-")
        })
        .filter_map(|(k, v)| Some((k.to_string(), v.to_str().ok()?.to_owned())))
        .collect();
    let mut values = json!({"job_id":job,"port":port,"path":mn_client_support::resources::decode_id(path)?,"query":r.uri().query().unwrap_or_default(),"headers":context,"websocket":ws});
    if ws {
        if !owner(c, r.headers()) {
            return Err(unauthorized());
        }
        values["path"] = mn_client_support::resources::decode_id(
            path.strip_prefix("ws/")
                .ok_or_else(|| Error::invalid("Invalid job WebSocket path."))?,
        )?
        .into();
        let protocols: Vec<_> = header_str(r.headers(), "sec-websocket-protocol")
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        values["subprotocols"] = json!(protocols);
        let (mut parts, _) = r.into_parts();
        let upgrade = WebSocketUpgrade::from_request_parts(&mut parts, &())
            .await
            .map_err(|_| Error::invalid("Invalid WebSocket upgrade."))?
            .max_message_size(MESSAGE_LIMIT);
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        let mut incoming = c.ipc.ui(values, Box::pin(ReceiverStream::new(rx))).await?;
        let first = incoming
            .next()
            .await
            .transpose()?
            .filter(|f| f.kind == "accepted")
            .ok_or_else(Error::protocol)?;
        let chosen = first.metadata.and_then(|m| {
            m.headers
                .into_iter()
                .find(|(k, _)| k == "subprotocol")
                .map(|(_, v)| v)
        });
        let upgrade = if let Some(p) = chosen.filter(|s| !s.is_empty()) {
            if !protocols.contains(&p) {
                return Err(Error::protocol());
            }
            upgrade.protocols([p])
        } else {
            upgrade
        };
        return Ok(upgrade.on_upgrade(move |socket| bridge(socket, tx, incoming)));
    }
    values["method"] = r.method().as_str().into();
    let mut body = r.into_body().into_data_stream();
    let outgoing = async_stream::stream! {while let Some(item)=body.next().await{let Ok(b)=item else{return};for part in b.chunks(FRAME_BYTES){yield frame("data",Bytes::copy_from_slice(part));}}};
    let mut incoming = c.ipc.ui(values, Box::pin(outgoing)).await?;
    let first = incoming
        .next()
        .await
        .transpose()?
        .filter(|f| f.kind == "headers")
        .and_then(|f| f.metadata)
        .ok_or_else(Error::protocol)?;
    let mut headers = vec![];
    for (k, v) in first.headers {
        if k.eq_ignore_ascii_case("x-mn-set-cookies") {
            for cookie in serde_json::from_str::<Vec<String>>(&v).map_err(|_| Error::protocol())? {
                headers.push(("set-cookie".into(), cookie));
            }
        } else {
            headers.push((k, v));
        }
    }
    let chunks =
        incoming.try_filter_map(|f| std::future::ready(Ok((f.kind == "data").then_some(f.data))));
    send(first.status, Body::from_stream(chunks), headers)
}
async fn bridge(socket: WebSocket, tx: tokio::sync::mpsc::Sender<Frame>, mut incoming: UiFrames) {
    let (mut writer, mut reader) = socket.split();
    let send = async move {
        while let Some(Ok(m)) = reader.next().await {
            let (kind, data) = match m {
                Message::Text(t) => ("text", Bytes::copy_from_slice(t.as_bytes())),
                Message::Binary(b) => ("binary", b),
                Message::Close(c) => {
                    let (code, reason) = c
                        .map(|f| (f.code, f.reason.to_string()))
                        .unwrap_or((1000, String::new()));
                    let _ = tx.send(close(code, &reason)).await;
                    return;
                }
                _ => continue,
            };
            if data.is_empty() {
                if tx.send(frame(kind, data)).await.is_err() {
                    return;
                }
            } else {
                let mut pieces = data.chunks(FRAME_BYTES).peekable();
                while let Some(p) = pieces.next() {
                    let name = if pieces.peek().is_some() {
                        format!("{kind}-part")
                    } else {
                        kind.into()
                    };
                    if tx
                        .send(frame(&name, Bytes::copy_from_slice(p)))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
            }
        }
        let _ = tx.send(close(1000, "")).await;
    };
    let receive = async move {
        let mut buf = Vec::new();
        let mut kind = String::new();
        let mut closing = (1011, "Runtime UI connection interrupted".to_string());
        while let Some(Ok(f)) = incoming.next().await {
            if f.kind == "close" {
                let meta: HashMap<_, _> = f.metadata.into_iter().flat_map(|m| m.headers).collect();
                closing = (
                    meta.get("code")
                        .and_then(|s| s.parse().ok())
                        .unwrap_or(1000),
                    meta.get("reason").cloned().unwrap_or_default(),
                );
                break;
            }
            let current = f.kind.trim_end_matches("-part");
            if !matches!(current, "text" | "binary")
                || (!kind.is_empty() && kind != current)
                || buf.len() + f.data.len() > MESSAGE_LIMIT
            {
                break;
            }
            kind = current.into();
            buf.extend_from_slice(&f.data);
            if !f.kind.ends_with("-part") {
                let data = std::mem::take(&mut buf);
                let message = if current == "text" {
                    let Ok(s) = String::from_utf8(data) else {
                        break;
                    };
                    Message::Text(s.into())
                } else {
                    Message::Binary(data.into())
                };
                kind.clear();
                if writer.send(message).await.is_err() {
                    return;
                }
            }
        }
        let _ = writer
            .send(Message::Close(Some(CloseFrame {
                code: closing.0,
                reason: closing.1.into(),
            })))
            .await;
    };
    tokio::pin!(receive);
    tokio::select! {_=send=>{},_= &mut receive=>{}}
}
