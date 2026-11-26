//! Public REST request validation and runtime argument projection.
mod fields;
use mn_client_support::{Error, Result};
use serde_json::{Value, json};
use std::{collections::HashMap, sync::LazyLock};
pub const SPEC: &str = include_str!("openapi.json");
pub static DOC: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(SPEC).expect("invalid embedded OpenAPI document"));
pub fn operation(template: &str, method: &str) -> &'static Value {
    &DOC["paths"][format!("/api/v1{}", template.replace(":path}", "}"))]
        [method.to_ascii_lowercase()]
}
pub fn query(raw: &str) -> Value {
    let mut values = serde_json::Map::new();
    for (name, value) in url::form_urlencoded::parse(raw.as_bytes()) {
        let field = values.entry(name.into_owned()).or_insert_with(|| json!([]));
        field
            .as_array_mut()
            .unwrap()
            .push(Value::String(value.into_owned()));
    }
    values.into()
}

pub(crate) fn invalid(issues: Vec<Value>) -> Error {
    let mut e = Error::new(
        422,
        "validation_failed",
        "One or more request fields are invalid.",
    );
    *e.details = json!({"errors":issues});
    e
}
pub(crate) fn malformed(bytes: &[u8], e: serde_json::Error) -> Error {
    let offset = if e.is_eof() {
        bytes.len()
    } else {
        bytes
            .split_inclusive(|b| *b == b'\n')
            .take(e.line().saturating_sub(1))
            .map(<[u8]>::len)
            .sum::<usize>()
            + e.column().saturating_sub(1)
    };
    let position = String::from_utf8_lossy(&bytes[..offset.min(bytes.len())])
        .chars()
        .count();
    invalid(vec![
        json!({"field":position.to_string(),"code":"json_invalid","message":"JSON decode error"}),
    ])
}

pub fn parameters(
    op: &Value,
    path: &mut Value,
    query: &mut Value,
    headers: &HashMap<String, String>,
) -> Result<()> {
    let mut issues = vec![];
    for p in op["parameters"].as_array().into_iter().flatten() {
        let name = p["name"].as_str().unwrap();
        let field = if p["in"] == "header" {
            format!("header.{name}")
        } else {
            name.into()
        };
        let schema = &p["schema"];
        let value = match p["in"].as_str() {
            Some("query") => query.get(name).cloned(),
            Some("path") => path.get(name).cloned(),
            Some("header") => headers.get(&name.to_ascii_lowercase()).map(|v| json!(v)),
            _ => None,
        };
        let mut value = match value {
            Some(v)
                if v.is_array()
                    && schema["type"] != "array"
                    && schema.pointer("/anyOf/0/type") != Some(&json!("array")) =>
            {
                v.as_array().unwrap().last().cloned().unwrap_or(Value::Null)
            }
            Some(v) => v,
            None if p["required"] == true => {
                fields::issue(&mut issues, &field, "missing", "Field required");
                continue;
            }
            None => {
                if p["in"] == "query" {
                    query[name] = schema["default"].clone();
                }
                continue;
            }
        };
        fields::project(schema, &mut value, &field, &mut issues);
        match p["in"].as_str() {
            Some("query") => query[name] = value,
            Some("path") => path[name] = value,
            _ => {}
        }
    }
    if issues.is_empty() {
        Ok(())
    } else {
        Err(invalid(issues))
    }
}
pub fn body(op: &Value, b: Option<&mut Value>) -> Result<()> {
    let mut issues = vec![];
    if let Some(schema) = op.pointer("/requestBody/content/application~1json/schema") {
        match b {
            Some(v) if !v.is_null() => fields::project(schema, v, "", &mut issues),
            _ if op["requestBody"]["required"] == true => {
                fields::issue(&mut issues, "", "missing", "Field required")
            }
            _ => {}
        }
    }
    if issues.is_empty() {
        Ok(())
    } else {
        Err(invalid(issues))
    }
}
/// The HTTP adapter uses the public parameter aliases. Core's descriptor also serves non-HTTP callers.
pub(crate) fn arguments(
    ep: &mn_client_support::resources::Endpoint,
    op: &Value,
    path: &Value,
    query: &Value,
    headers: &HashMap<String, String>,
    body: Option<&Value>,
    principal: &str,
) -> Value {
    let mut args = ep.arguments(path, query, headers, body, principal);
    for (key, binding) in &ep.parameters {
        if matches!(
            binding.source.as_str(),
            "upload" | "context" | "response" | "principal"
        ) {
            continue;
        }
        let param = op["parameters"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|param| {
                let name = param["name"].as_str().unwrap_or("");
                name.eq_ignore_ascii_case(&binding.name)
                    || name.replace('-', "_").eq_ignore_ascii_case(key)
                    || (name == "status"
                        && matches!(key.as_str(), "request_status" | "service_status"))
            });
        let value = if let Some(p) = param {
            let name = p["name"].as_str().unwrap();
            match p["in"].as_str() {
                Some("header") => headers
                    .get(&name.to_ascii_lowercase())
                    .map(|h| json!(h))
                    .unwrap_or_else(|| p["schema"]["default"].clone()),
                Some("query") => query[name].clone(),
                _ => path[name].clone(),
            }
        } else if binding.source == "body" {
            body.cloned().unwrap_or_default()
        } else {
            Value::Null
        };
        args[key] = value;
    }
    args
}

pub(crate) fn validate_request(params: Result<()>, body: Result<()>) -> Result<()> {
    match (params, body) {
        (Err(mut params), Err(body)) => {
            if let (Some(fields), Some(more)) = (
                params.details["errors"].as_array_mut(),
                body.details["errors"].as_array(),
            ) {
                fields.extend(
                    more.iter()
                        .take(100usize.saturating_sub(fields.len()))
                        .cloned(),
                );
            }
            Err(params)
        }
        (a, b) => a.and(b),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn query_arrays_accept_single_and_repeated_tags() {
        for (raw, expected) in [
            ("tag=one", json!(["one"])),
            ("tag=one&tag=two", json!(["one", "two"])),
        ] {
            let mut query = query(raw);
            parameters(
                operation("/services", "get"),
                &mut json!({}),
                &mut query,
                &HashMap::new(),
            )
            .unwrap();
            assert_eq!(query["tag"], expected);
            assert_eq!(query["page_size"], 50);
        }
    }
}

#[cfg(test)]
mod contract_tests {
    use super::*;
    use mn_client_support::resources::endpoint;
    #[test]
    fn nullable_blueprint_models_receive_python_model_defaults() {
        let op = operation("/blueprints/{blueprint_id}/runs", "post");
        let mut request = json!({"fake_llm":"on","inputs":{"threshold":3}});
        body(op, Some(&mut request)).unwrap();
        assert_eq!(request["fake_llm"], true);
        assert_eq!(request["admission_policy"], "start_now");
        assert_eq!(request["secret_environment"], json!({}));
        assert_eq!(request.get("owner_node"), Some(&Value::Null));
        request["schedule"] = json!({});
        request["admission_policy"] = json!("enqueue_if_busy");
        assert_eq!(
            body(op, Some(&mut request)).unwrap_err().details["errors"][0]["code"],
            "value_error"
        );
    }
    #[test]
    fn aliases_and_null_defaults_survive_the_rest_to_grpc_boundary() {
        let ep = endpoint("list_services").unwrap();
        let op = operation(&ep.path, &ep.method);
        let mut q = query("status=passing&tag=one&tag=two");
        let h = HashMap::new();
        let mut path = json!({});
        parameters(op, &mut path, &mut q, &h).unwrap();
        let args = arguments(ep, op, &path, &q, &h, None, "authenticated");
        assert_eq!(args["service_status"], "passing");
        assert_eq!(args["tag"], json!(["one", "two"]));
        assert_eq!(args.get("page_token"), Some(&Value::Null));
    }
}
