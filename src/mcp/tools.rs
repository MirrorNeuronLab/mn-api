use super::Session;
use mn_client_support::{Error, Result};
use serde_json::{Value, json};
use std::sync::LazyLock;
use uuid::Uuid;

static TOOLS: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(include_str!("../mcp-tools.json")).unwrap());

pub(super) fn specs(d: &Value) -> Result<Value> {
    let invalid = |message| Error::new(422, "MN_MCP_DECLARATION_INVALID", message);
    if d["profile"] != TOOLS["profile"] {
        return Err(invalid("mcp.profile must be mn.mcp.supervision.v1"));
    }
    let manual = d["manual"].as_str().unwrap_or_default();
    if !manual.starts_with("@/")
        || manual.contains('\\')
        || manual.split('/').any(|part| part == "..")
    {
        return Err(invalid(
            "mcp.manual must reference a bundle-local Markdown file",
        ));
    }
    if !manual.ends_with(".md") {
        return Err(invalid("mcp.manual must be Markdown"));
    }
    if d.get("tools").is_some_and(|tools| !tools.is_object()) {
        return Err(invalid("mcp.tools must be an object"));
    }
    let mut specs = TOOLS["common"].clone();
    if d["mailbox"] == true || d["mailbox"].is_object() {
        specs
            .as_object_mut()
            .unwrap()
            .extend(TOOLS["mailbox"].as_object().unwrap().clone());
    }
    if let Some(domain) = d["tools"].as_object() {
        for (n, raw) in domain {
            let mut item = raw.clone();
            let mut envelope = TOOLS["result"].clone();
            envelope["allOf"] = json!([{"if":{"properties":{"status":{"const":"completed"}}},"then":{"properties":{"data":raw["outputSchema"]}}}]);
            item["outputSchema"] = envelope;
            specs[n] = item;
        }
    }
    Ok(specs)
}
pub(super) fn list(s: &Session, d: &Value) -> Result<Vec<Value>> {
    let specs = specs(d)?;
    let mut rows = Vec::new();
    for (name, spec) in specs.as_object().ok_or_else(Error::protocol)? {
        let read_only = spec["effect"] == "read";
        rows.push(json!({
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
            "outputSchema": spec["outputSchema"],
            "annotations": {
                "readOnlyHint": read_only,
                "destructiveHint": !read_only,
                "idempotentHint": true
            },
            "_meta": {"mn.mcp.profile": TOOLS["profile"], "mn.mcp.effect": spec["effect"]}
        }));
    }
    let idx = match s.mode.as_str() {
        "response" => 1,
        "agent" => 2,
        _ => 0,
    };
    for tool in TOOLS["conversations"][idx].as_array().into_iter().flatten() {
        let mut tool = tool.clone();
        tool["outputSchema"] = TOOLS["result"].clone();
        rows.push(tool);
    }
    Ok(rows)
}
pub(super) fn envelope(name: &str, identity: &Value, id: &str, data: Value) -> Result<Value> {
    let mut payload = if data["schema_version"] == "mn.mcp.tool_result.v1" {
        data
    } else {
        json!({
            "schema_version": "mn.mcp.tool_result.v1", "tool": name, "request_id": id,
            "job_id": identity["job_id"], "blueprint_id": identity["blueprint_id"],
            "run_id": identity["run_id"], "status": "completed", "operation_id": null,
            "data": data, "error": null
        })
    };
    if payload["request_id"].as_str().is_none_or(str::is_empty) {
        payload["request_id"] = id.into();
    }
    if payload["job_id"] != identity["job_id"]
        || payload["blueprint_id"] != identity["blueprint_id"]
    {
        return Err(Error::protocol());
    }
    let text = super::json::text(&payload);
    if text.len() > 256 * 1024 {
        return Err(Error::invalid("Tool response exceeds 256 KiB"));
    }
    Ok(
        json!({"content":[{"type":"text","text":text}],"structuredContent":payload,"isError":!payload["error"].is_null()}),
    )
}

fn invalid_call(name: &str, identity: &Value, request_id: &str) -> Result<Value> {
    let message = "The request does not match this co-worker's MCP contract.";
    if name == "get_operation_status" {
        return Err(Error::new(422, "MN_MCP_REQUEST_INVALID", message));
    }
    let payload = json!({
        "schema_version": "mn.mcp.tool_result.v1", "tool": name, "request_id": request_id,
        "job_id": identity["job_id"], "blueprint_id": identity["blueprint_id"], "run_id": identity["run_id"],
        "status": "failed", "operation_id": null, "data": {},
        "error": {"code": "invalid_request", "message": message, "retryable": false}
    });
    Ok(
        json!({"content":[{"type":"text","text":message}],"structuredContent":payload,"isError":true}),
    )
}

pub(super) async fn call(s: &Session, p: &Value) -> Result<Value> {
    let name = p["name"]
        .as_str()
        .ok_or_else(|| Error::invalid("Missing tool name."))?;
    let args = p.get("arguments").cloned().unwrap_or_else(|| json!({}));
    let id = args["request_id"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let identity = s.supervision("identity", json!({})).await?;
    let decl = s.supervision("declaration", json!({})).await?;
    let all = match list(s, &decl) {
        Ok(tools) => tools,
        Err(e) if e.status == 422 => return invalid_call(name, &identity, &id),
        Err(e) => return Err(e),
    };
    let Some(spec) = all.iter().find(|tool| tool["name"] == name) else {
        return invalid_call(name, &identity, &id);
    };
    if !jsonschema::validator_for(&spec["inputSchema"])
        .map_err(|_| Error::protocol())?
        .is_valid(&args)
    {
        return invalid_call(name, &identity, &id);
    }
    let data = match name {
        "get_capabilities" => {
            let mut v = s.supervision("capabilities", json!({})).await?;
            v["profile"] = TOOLS["profile"].clone();
            v["tools"] = all.clone().into();
            v
        }
        "ask_job" => {
            let mut kwargs = json!({"conversation_id":args["conversation_id"],"request_id":args["request_id"],"assistance_task":args["assistance_task"]});
            let method = if args["background"] == true {
                kwargs["action"] = args["reply_action"].as_str().unwrap_or("start").into();
                "response_work_command"
            } else {
                "ask_job"
            };
            s.context(method, json!([args["question"]]), kwargs).await?
        }
        "get_job_turn" => {
            s.context("get_job_turn", json!([args["turn_id"]]), json!({}))
                .await?
        }
        _ => {
            let mut meta = json!({});
            if name == "start_run"
                && let Some(v) = p["_meta"].get("mn.launch")
            {
                meta["mn.launch"] = v.clone();
            }
            s.supervision(
                "invoke",
                json!({"name":name,"arguments":args,"metadata":meta}),
            )
            .await?
        }
    };
    if data["result_type"] == "input_required" {
        return Ok(data);
    }
    let res = envelope(name, &identity, &id, data)?;
    if !jsonschema::validator_for(&spec["outputSchema"])
        .map_err(|_| Error::protocol())?
        .is_valid(&res["structuredContent"])
    {
        return Err(Error::protocol());
    }
    Ok(res)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_tools_still_return_a_job_bound_tool_result() {
        let identity = json!({"job_id":"j", "blueprint_id":"b"});
        let failed = invalid_call("unknown", &identity, "r").unwrap();
        let payload = &failed["structuredContent"];
        assert_eq!(failed["isError"], true);
        assert_eq!(failed["content"][0]["text"], payload["error"]["message"]);
        assert_eq!(payload["error"]["code"], "invalid_request");
        assert_eq!(payload["job_id"], "j");
        assert_eq!(payload["request_id"], "r");
        assert!(invalid_call("get_operation_status", &identity, "r").is_err());
    }
}
