//! Parameter coercion and request validation against the public v1 schema.
use mn_client_support::{Error, Result};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{Arc, LazyLock, Mutex},
};
pub const SPEC: &str = include_str!("openapi.json");
pub static DOC: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(SPEC).expect("invalid embedded OpenAPI document"));
static VALIDATORS: LazyLock<Mutex<HashMap<String, Arc<jsonschema::Validator>>>> =
    LazyLock::new(Default::default);
pub fn operation(template: &str, method: &str) -> &'static Value {
    &DOC["paths"][format!("/api/v1{}", template.replace(":path}", "}"))]
        [method.to_ascii_lowercase()]
}
fn invalid(name: &str) -> Error {
    let mut e = Error::new(
        422,
        "input_validation_failed",
        "One or more input fields failed validation.",
    );
    e.details = json!({"errors":[{"field":name,"message":"Invalid or missing value."}]});
    e
}
fn validate(schema: &Value, value: &Value, name: &str) -> Result<()> {
    let key = schema.to_string();
    let mut cache = VALIDATORS.lock().unwrap();
    let validator = match cache.get(&key) {
        Some(v) => v.clone(),
        None => {
            let v = Arc::new(
                jsonschema::validator_for(
                    &json!({"components":DOC["components"],"allOf":[schema]}),
                )
                .expect("invalid bundled resource schema"),
            );
            cache.insert(key, v.clone());
            v
        }
    };
    drop(cache);
    if validator.is_valid(value) {
        Ok(())
    } else {
        Err(invalid(name))
    }
}
fn parse(schema: &Value, s: &str) -> Value {
    let ty = schema
        .get("type")
        .or_else(|| schema.pointer("/anyOf/0/type"))
        .and_then(Value::as_str);
    match ty {
        Some("integer") => s
            .parse::<i64>()
            .map(Value::from)
            .unwrap_or_else(|_| json!(s)),
        Some("number") => s
            .parse::<f64>()
            .ok()
            .filter(|n| n.is_finite())
            .map(Value::from)
            .unwrap_or_else(|| json!(s)),
        Some("boolean") => match s.to_ascii_lowercase().as_str() {
            "1" | "true" | "on" | "yes" => true.into(),
            "0" | "false" | "off" | "no" => false.into(),
            _ => json!(s),
        },
        _ => json!(s),
    }
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

fn parameter(schema: &Value, value: &Value) -> Value {
    let shape = schema
        .get("anyOf")
        .and_then(Value::as_array)
        .and_then(|options| options.iter().find(|s| s["type"] != "null"))
        .unwrap_or(schema);
    let values = value
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_else(|| std::slice::from_ref(value));
    if shape["type"] == "array" {
        values
            .iter()
            .map(|v| parse(&shape["items"], v.as_str().unwrap_or_default()))
            .collect()
    } else {
        parse(
            shape,
            values.last().and_then(Value::as_str).unwrap_or_default(),
        )
    }
}
pub fn parameters(
    op: &Value,
    path: &mut Value,
    query: &mut Value,
    headers: &HashMap<String, String>,
) -> Result<()> {
    for p in op["parameters"].as_array().into_iter().flatten() {
        let name = p["name"].as_str().unwrap();
        let schema = &p["schema"];
        let field = match p["in"].as_str() {
            Some("query") => query.get(name).cloned(),
            Some("path") => path.get(name).cloned(),
            Some("header") => headers.get(&name.to_ascii_lowercase()).map(|s| json!(s)),
            _ => None,
        };
        let v = match field {
            Some(v) => parameter(schema, &v),
            None if p["required"] == true => return Err(invalid(name)),
            None => match schema.get("default") {
                Some(v) => v.clone(),
                None => continue,
            },
        };
        validate(schema, &v, name)?;
        match p["in"].as_str() {
            Some("query") => query[name] = v,
            Some("path") => path[name] = v,
            _ => {}
        }
    }
    Ok(())
}
pub fn body(op: &Value, b: Option<&Value>) -> Result<()> {
    if let Some(schema) = op.pointer("/requestBody/content/application~1json/schema") {
        if let Some(v) = b {
            validate(schema, v, "body")?;
        } else if op["requestBody"]["required"] == true {
            return Err(invalid("body"));
        }
    }
    Ok(())
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
