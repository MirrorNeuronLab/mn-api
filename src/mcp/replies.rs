use super::{Session, tools::envelope};
use crate::http::send;
use axum::{body::Body, response::Response};
use futures_util::TryStreamExt;
use mn_client_support::{Error, Result, adapter::Adapter};
use serde_json::{Value, json};
use std::time::Duration;
use tokio::time::{Instant, timeout_at};
use uuid::Uuid;

struct CancelStream {
    ipc: Adapter,
    args: Value,
    done: bool,
}
impl CancelStream {
    fn complete(&mut self) {
        self.done = true;
    }
}
impl Drop for CancelStream {
    fn drop(&mut self) {
        if !self.done {
            let mut args = self.args.clone();
            args["kwargs"]["control"]["action"] = "cancel".into();
            let ipc = self.ipc.clone();
            tokio::spawn(async move {
                let _ = ipc.command("mcp.context", args, 2).await;
            });
        }
    }
}
fn delta<'a>(part: &'a Value, request_id: &str, cursor: u64) -> Result<(&'a str, u64)> {
    let delta = part["delta"].as_str().ok_or_else(Error::protocol)?;
    let next = cursor + delta.chars().count() as u64;
    if part["schema_version"] != "mn.mcp.job_answer_stream.v1"
        || part["request_id"] != request_id
        || part["failed"] == true
        || part["cancelled"] == true
        || delta.len() > 4000
        || part["cursor"] != next
        || next > 12000
    {
        return Err(Error::protocol());
    }
    Ok((delta, next))
}

pub(super) async fn stream(s: Session, p: Value, id: Value) -> Result<Response> {
    let args = &p["arguments"];
    let request = args["request_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let question = args["question"]
        .as_str()
        .ok_or_else(|| Error::invalid("Missing question."))?;
    let identity = s.supervision("identity", json!({})).await?;
    let mut kwargs = json!({
        "request_id": request,
        "conversation_id": args["conversation_id"],
        "control": {"action": "start", "cursor": 0}
    });
    if !args["assistance_task"].is_null() {
        kwargs["assistance_task"] = args["assistance_task"].clone();
    }
    let mut guard = CancelStream {
        ipc: s.ipc,
        args: json!({"method": "response_stream_command", "job_id": s.job, "args": [question], "kwargs": kwargs}),
        done: false,
    };
    let data = async_stream::try_stream! {
        let deadline = Instant::now() + Duration::from_secs(90);
        let progress_token = &p["_meta"]["progressToken"];
        let mut cursor = 0;
        let mut sequence = 0;
        loop {
            let command = guard.ipc.command("mcp.context", guard.args.clone(), 90);
            let part = timeout_at(deadline, command).await.map_err(|_| Error::protocol())??;
            let (text, next) = delta(&part, &request, cursor)?;
            cursor = next;
            guard.args["kwargs"]["control"] = json!({"action": "read", "cursor": cursor});
            if !text.is_empty() && !progress_token.is_null() {
                sequence += 1;
                let message = json!({
                    "schema_version": "mn.mcp.job_answer_delta.v1",
                    "request_id": request, "sequence": sequence, "delta": text
                });
                let event = json!({
                    "jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": progress_token, "progress": sequence, "message": message.to_string()}
                });
                yield format!("event: message\ndata: {event}\n\n");
            }
            if part["done"] == true {
                let answer = &part["response"];
                if answer["schema_version"] != "mn.mcp.job_answer.v1" {
                    Err(Error::protocol())?;
                }
                let result = envelope("ask_job", &identity, &request, answer.clone())?;
                let event = json!({"jsonrpc": "2.0", "id": id, "result": result});
                guard.complete();
                yield format!("event: message\ndata: {event}\n\n");
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    };
    send(
        200,
        Body::from_stream(data.map_err(|e: Error| e)),
        [
            ("content-type".into(), "text/event-stream".into()),
            ("cache-control".into(), "no-store".into()),
        ],
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delta_limits_use_bytes_for_payload_size_and_characters_for_the_cursor() {
        let mut part = json!({"schema_version": "mn.mcp.job_answer_stream.v1",
            "request_id": "r", "delta": "é", "cursor": 5});
        assert_eq!(delta(&part, "r", 4).unwrap(), ("é", 5));
        assert!(delta(&part, "other", 4).is_err());
        part["cursor"] = 6.into();
        assert!(delta(&part, "r", 4).is_err());
        part["delta"] = "é".repeat(2001).into();
        part["cursor"] = 2001.into();
        assert!(delta(&part, "r", 0).is_err());
    }
}
