use super::*;
use crate::grants::Store;
use base64::{Engine, engine::general_purpose::STANDARD};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, sync::LazyLock};
use url::Url;
static TOOLS: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(include_str!("mcp-tools.json")).unwrap());
pub async fn admin(c: &Config, r: Request, job: &str, kind: &str) -> Result<Response> {
    let base = origin(&r);
    let method = r.method().as_str().to_owned();
    let job = &resources::decode_id(job)?;
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
            let b: Value = serde_json::from_slice(
                &to_bytes(r.into_body(), c.limit)
                    .await
                    .map_err(|_| too_large())?,
            )
            .map_err(|_| Error::invalid("Invalid access policy."))?;
            schema::body(
                schema::operation("/jobs/{job_id}/mcp-access", "PUT"),
                Some(&b),
            )?;
            let enabled = b["enabled"]
                .as_bool()
                .ok_or_else(|| Error::invalid("Invalid enabled flag."))?;
            if enabled && (record["status"] == "archived" || c.token.is_empty()) {
                return Err(Error::new(
                    409,
                    "conflict",
                    "Configure MN_API_TOKEN and restore the co-worker before enabling connections.",
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
            if !valid || raw.chars().any(|ch| ch.is_whitespace() || ch.is_control()) {
                return Err(Error::invalid(
                    "Use an HTTPS or loopback MCP URL for this job.",
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
struct Session {
    ipc: Adapter,
    job: String,
    internal: bool,
    mode: String,
}
impl Session {
    async fn context(&self, method: &str, args: Value, kwargs: Value) -> Result<Value> {
        self.ipc
            .command(
                "mcp.context",
                json!({"job_id":self.job,"method":method,"args":args,"kwargs":kwargs}),
                120,
            )
            .await
    }
    async fn supervision(&self, method: &str, mut fields: Value) -> Result<Value> {
        fields["job_id"] = self.job.clone().into();
        fields["internal"] = self.internal.into();
        fields["method"] = method.into();
        self.ipc.command("mcp.supervision", fields, 120).await
    }
}
pub async fn protocol(c: &Config, r: Request, job: &str) -> Result<Response> {
    let job = resources::decode_id(job)?;
    let auth = if owner(c, r.headers()) {
        Some(true)
    } else {
        Store::open(&c.home)?.authority(&job, bearer(r.headers()))?
    };
    let Some(internal) = auth else {
        let mut e = unauthorized();
        let policy = Store::open(&c.home)?.policy(&job)?;
        let source = policy["server_url"]
            .as_str()
            .and_then(|s| Url::parse(s).ok())
            .map(|u| u.origin().ascii_serialization())
            .unwrap_or_else(|| origin(&r));
        e.headers.insert("www-authenticate".into(),format!("Bearer resource_metadata=\"{source}/.well-known/oauth-protected-resource/api/v1/jobs/{}/mcp\"",resources::resource_id(&job)));
        return Err(e);
    };
    if r.method() != "POST" {
        return Err(Error::new(
            405,
            "method_not_allowed",
            "Use MCP Streamable HTTP POST.",
        ));
    }
    let body: Value = serde_json::from_slice(
        &to_bytes(r.into_body(), c.limit)
            .await
            .map_err(|_| too_large())?,
    )
    .map_err(|_| Error::invalid("Invalid JSON-RPC request."))?;
    let id = &body["id"];
    if body["jsonrpc"] != "2.0" || !body["method"].is_string() {
        return rpc_error(id, -32600, "Invalid Request", Value::Null);
    }
    if body["method"]
        .as_str()
        .is_some_and(|m| m.starts_with("notifications/"))
        && id.is_null()
    {
        return send(202, Body::empty(), []);
    }
    let (ipc, _) = c.ipc.pin().await?;
    let mut s = Session {
        ipc,
        job,
        internal,
        mode: String::new(),
    };
    s.mode = s
        .context("response_mode", json!([]), json!({}))
        .await?
        .as_str()
        .unwrap_or("base")
        .to_owned();
    let p = &body["params"];
    let method = body["method"].as_str().unwrap();
    if method == "tools/call"
        && p["name"] == "ask_job"
        && p["arguments"]["stream"] == true
        && s.mode == "response"
    {
        return stream_reply(s, p.clone(), id.clone()).await;
    }
    let result = match method {
        "initialize" | "server/discover" => Ok(
            json!({"protocolVersion":p["protocolVersion"].as_str().filter(|v|matches!(*v,"2025-03-26"|"2025-06-18"|"2025-11-25")).unwrap_or("2026-07-28"),"serverInfo":{"name":"MirrorNeuron job supervision","version":env!("CARGO_PKG_VERSION")},"capabilities":{"tools":{},"resources":{},"extensions":{"io.modelcontextprotocol/skills":{"directoryRead":true}}}}),
        ),
        "ping" => Ok(json!({})),
        "tools/list" => {
            Ok(json!({"tools":tools(&s,&s.supervision("declaration",json!({})).await?)?}))
        }
        "tools/call" => call_tool(&s, p).await,
        "resources/templates/list" => Ok(json!({"resourceTemplates":[]})),
        "skills/list"
        | "skills/get"
        | "resources/read"
        | "resources/list"
        | "resources/directory/read" => skill_resources(&s, method, p).await,
        _ => return rpc_error(id, -32601, "Method not found", Value::Null),
    };
    match result {
        Ok(v) => json_response(200, json!({"jsonrpc":"2.0","id":id,"result":v})),
        Err(e) => rpc_error(
            id,
            if e.status == 422 { -32602 } else { -32000 },
            &e.message,
            json!({"code":e.code,"retryable":e.status>=500}),
        ),
    }
}
fn rpc_error(id: &Value, code: i64, msg: &str, data: Value) -> Result<Response> {
    json_response(
        200,
        json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":msg,"data":data}}),
    )
}
fn specs(d: &Value) -> Value {
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
    specs
}
fn tools(s: &Session, d: &Value) -> Result<Vec<Value>> {
    let mut rows:Vec<_>=specs(d).as_object().unwrap().iter().map(|(name,spec)|json!({"name":name,"description":spec["description"],"inputSchema":spec["inputSchema"],"outputSchema":spec["outputSchema"],"annotations":{"readOnlyHint":spec["effect"]=="read","destructiveHint":spec["effect"]!="read","idempotentHint":true},"_meta":{"mn.mcp.profile":TOOLS["profile"],"mn.mcp.effect":spec["effect"]}})).collect();
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
fn envelope(name: &str, identity: &Value, id: &str, data: Value) -> Result<Value> {
    let payload = if data["schema_version"] == "mn.mcp.tool_result.v1" {
        data
    } else {
        json!({"schema_version":"mn.mcp.tool_result.v1","tool":name,"request_id":id,"job_id":identity["job_id"],"blueprint_id":identity["blueprint_id"],"run_id":identity["run_id"],"operation_id":null,"status":"completed","data":data,"error":null})
    };
    if payload["job_id"] != identity["job_id"]
        || payload["blueprint_id"] != identity["blueprint_id"]
    {
        return Err(Error::protocol());
    }
    Ok(
        json!({"content":[{"type":"text","text":payload.to_string()}],"structuredContent":payload,"isError":payload["status"]=="failed"}),
    )
}
async fn call_tool(s: &Session, p: &Value) -> Result<Value> {
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
    let all = tools(s, &decl)?;
    let spec = all
        .iter()
        .find(|t| t["name"] == name)
        .ok_or_else(|| Error::invalid("Undeclared MCP tool."))?;
    if !jsonschema::validator_for(&spec["inputSchema"])
        .map_err(|_| Error::protocol())?
        .is_valid(&args)
    {
        return Err(Error::invalid("Invalid tool arguments."));
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
async fn skill_resources(s: &Session, method: &str, p: &Value) -> Result<Value> {
    let d = s.supervision("declaration", json!({})).await?;
    let identity = s.supervision("identity", json!({})).await?;
    let bp = identity["blueprint_id"]
        .as_str()
        .ok_or_else(Error::protocol)?;
    let name = bp
        .to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    let name = name.chars().take(64).collect::<String>();
    let name = if name.is_empty() { "co-worker" } else { &name };
    let base = format!("skill://{name}");
    let description = format!("Supervise the {bp} co-worker through its Job-bound MCP tools.");
    let skill = format!(
        "---\nname: {name}\ndescription: {}\n---\n\n# Co-worker supervision\n\nRead references/user_manual.md for the co-worker’s mission, controls and limits. Use the declared tools to inspect work. Start or change runs only on an explicit request. Never request or place launch credentials in tool arguments or conversation history.\n",
        json!(description)
    );
    let mut bundles = vec![(
        base.clone(),
        name.to_owned(),
        description,
        BTreeMap::from([
            ("SKILL.md".to_owned(), skill.into_bytes()),
            (
                "references/user_manual.md".into(),
                d["manual_content"]
                    .as_str()
                    .unwrap_or_default()
                    .as_bytes()
                    .to_vec(),
            ),
            (
                "references/tools.json".into(),
                specs(&d).to_string().into_bytes(),
            ),
        ]),
    )];
    if d["agent_skills_prepared"] == true {
        let extra = s.supervision("enabled_skill_bundles", json!({})).await?;
        for (n, b) in extra.as_object().ok_or_else(Error::protocol)? {
            let mut files = BTreeMap::new();
            for (path, v) in b["files"].as_object().ok_or_else(Error::protocol)? {
                let data = STANDARD
                    .decode(v["data"].as_str().ok_or_else(Error::protocol)?)
                    .map_err(|_| Error::protocol())?;
                if v["sha256"] != format!("{:x}", Sha256::digest(&data)) {
                    return Err(Error::protocol());
                }
                files.insert(path.clone(), data);
            }
            bundles.push((
                format!("skill://dependencies/{n}"),
                n.clone(),
                n.clone(),
                files,
            ));
        }
    }
    let mut entries = vec![];
    let mut resources = vec![];
    let mut contents = BTreeMap::new();
    for (base, name, desc, files) in bundles {
        let mut refs = vec![];
        for (path, bytes) in files {
            let uri = format!(
                "{base}/{}",
                path.split('/')
                    .map(resources::resource_id)
                    .collect::<Vec<_>>()
                    .join("/")
            );
            let mime = if path.ends_with(".md") {
                "text/markdown"
            } else if path.ends_with(".json") {
                "application/json"
            } else {
                "application/octet-stream"
            };
            refs.push(json!({"uri":uri,"digest":format!("sha256:{:x}",Sha256::digest(&bytes)),"size":bytes.len()}));
            resources.push(json!({"uri":uri,"name":path.rsplit('/').next(),"mimeType":mime}));
            let content = match String::from_utf8(bytes) {
                Ok(s) => json!({"uri":uri,"mimeType":mime,"text":s}),
                Err(e) => json!({"uri":uri,"mimeType":mime,"blob":STANDARD.encode(e.as_bytes())}),
            };
            contents.insert(uri, content);
        }
        entries.push(json!({"uri":format!("{base}/SKILL.md"),"frontmatter":{"name":name,"description":desc},"resources":refs}));
    }
    let bad = || Error::invalid("Unknown co-worker skill resource.");
    if p["cursor"].is_string() {
        return Err(bad());
    }
    let mut value = match method {
        "skills/list" => json!({"skills":entries}),
        "skills/get" => {
            json!({"skill":entries.iter().find(|r|r["uri"]==p["uri"]).ok_or_else(bad)?})
        }
        "resources/read" => {
            json!({"contents":[contents.get(p["uri"].as_str().ok_or_else(bad)?).ok_or_else(bad)?]})
        }
        "resources/directory/read" => {
            let prefix = format!(
                "{}/",
                p["uri"].as_str().ok_or_else(bad)?.trim_end_matches('/')
            );
            resources.retain(|r| r["uri"].as_str().is_some_and(|s| s.starts_with(&prefix)));
            if resources.is_empty() {
                return Err(bad());
            }
            json!({"resources":resources})
        }
        _ => json!({"resources":resources}),
    };
    value["ttlMs"] = 0.into();
    value["cacheScope"] = "private".into();
    Ok(value)
}
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
async fn stream_reply(s: Session, p: Value, id: Value) -> Result<Response> {
    let args = &p["arguments"];
    let request = args["request_id"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let question = args["question"]
        .as_str()
        .ok_or_else(|| Error::invalid("Missing question."))?;
    let identity = s.supervision("identity", json!({})).await?;
    let mut guard = CancelStream {
        ipc: s.ipc,
        args: json!({"method":"response_stream_command","job_id":s.job,"args":[question],"kwargs":{"request_id":request,"conversation_id":args["conversation_id"],"assistance_task":args["assistance_task"],"control":{"action":"start","cursor":0}}}),
        done: false,
    };
    let data = async_stream::try_stream! {
        let start=Instant::now();let mut cursor=0u64;let mut seq=0;
        loop {if start.elapsed().as_secs()>90{Err(Error::protocol())?;}let part=guard.ipc.command("mcp.context",guard.args.clone(),90).await?;
            let delta=part["delta"].as_str().ok_or_else(Error::protocol)?;let next=cursor+delta.chars().count() as u64;if part["schema_version"]!="mn.mcp.job_answer_stream.v1"||part["request_id"]!=request||part["failed"]==true||part["cancelled"]==true||delta.len()>4000||part["cursor"]!=next||next>12000{Err(Error::protocol())?;}cursor=next;
            if !delta.is_empty(){seq+=1;let msg=json!({"schema_version":"mn.mcp.job_answer_delta.v1","request_id":request,"sequence":seq,"delta":delta});let event=json!({"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":p["_meta"]["progressToken"],"progress":seq,"message":msg.to_string()}});yield format!("event: message\ndata: {event}\n\n");}
            if part["done"]==true{let answer=&part["response"];if answer["schema_version"]!="mn.mcp.job_answer.v1"{Err(Error::protocol())?;}let result=envelope("ask_job",&identity,&request,answer.clone())?;let event=json!({"jsonrpc":"2.0","id":id,"result":result});guard.complete();yield format!("event: message\ndata: {event}\n\n");break;}
            guard.args["kwargs"]["control"]=json!({"action":"read","cursor":cursor});tokio::time::sleep(std::time::Duration::from_millis(50)).await;
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
