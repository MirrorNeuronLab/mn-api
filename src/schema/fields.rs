//! Interpret the constraints used by the public REST input models.
use super::DOC;
use regex::Regex;
use serde_json::{Value, json};

pub(super) type Issues = Vec<Value>;

pub(super) fn issue(issues: &mut Issues, path: &str, code: &str, message: &str) {
    if issues.len() < 100 {
        issues.push(json!({"field":path,"code":code,"message":message}));
    }
}

fn child(path: &str, name: &str) -> String {
    if path.is_empty() {
        name.to_owned()
    } else {
        format!("{path}.{name}")
    }
}
fn member<'a>(schema: &'a Value, value: &Value) -> &'a Value {
    schema["anyOf"]
        .as_array()
        .and_then(|options| {
            options
                .iter()
                .find(|s| (s["type"] == "null") == value.is_null())
        })
        .unwrap_or(schema)
}

pub(super) fn project(schema: &Value, value: &mut Value, path: &str, issues: &mut Issues) {
    if let Some(options) = schema["anyOf"].as_array() {
        if options.iter().any(|s| s["type"] == "null") {
            let selected = member(schema, value);
            if selected["type"] != "null" {
                project(selected, value, path, issues);
            }
        } else {
            // The retry override union is StrictInt | StrictStr. Preserve each branch location on failure.
            if !value.is_i64() && !value.is_u64() && !value.is_string() {
                issue(
                    issues,
                    &child(path, "int"),
                    "int_type",
                    "Input should be a valid integer",
                );
                issue(
                    issues,
                    &child(path, "str"),
                    "string_type",
                    "Input should be a valid string",
                );
            }
        }
        return;
    }
    if let Some(reference) = schema["$ref"].as_str() {
        project(
            DOC.pointer(reference.trim_start_matches('#'))
                .expect("bundled schema reference"),
            value,
            path,
            issues,
        );
        return;
    }

    if value.is_null() && schema["type"] == "null" {
        return;
    }
    if let Some(options) = schema["enum"].as_array() {
        if !options.contains(value) {
            literal(path, options, issues);
        }
        return;
    }
    if let Some(constant) = schema.get("const") {
        if value != constant {
            literal(path, std::slice::from_ref(constant), issues);
        }
        return;
    }
    match schema["type"].as_str() {
        Some("object") => object(schema, value, path, issues),
        Some("array") => {
            if let Some(items) = value.as_array_mut() {
                length(schema, items.len(), path, "List", "item", issues);
                for (idx, item) in items.iter_mut().enumerate() {
                    project(
                        &schema["items"],
                        item,
                        &child(path, &idx.to_string()),
                        issues,
                    );
                }
            } else {
                issue(issues, path, "list_type", "Input should be a valid list");
            }
        }
        Some("integer" | "number" | "boolean") => scalar(schema, value, path, issues),
        Some("string") => {
            let Some(text) = value.as_str() else {
                issue(
                    issues,
                    path,
                    "string_type",
                    "Input should be a valid string",
                );
                return;
            };
            let size = text.chars().count() as u64;
            for (key, code, word) in [
                ("minLength", "string_too_short", "at least"),
                ("maxLength", "string_too_long", "at most"),
            ] {
                if let Some(n) = schema[key].as_u64()
                    && (if key == "minLength" {
                        size < n
                    } else {
                        size > n
                    })
                {
                    issue(
                        issues,
                        path,
                        code,
                        &format!(
                            "String should have {word} {n} character{}",
                            if n == 1 { "" } else { "s" }
                        ),
                    );
                    return;
                }
            }
            if let Some(p) = schema["pattern"].as_str()
                && !Regex::new(p).expect("bundled field pattern").is_match(text)
            {
                issue(
                    issues,
                    path,
                    "string_pattern_mismatch",
                    &format!("String should match pattern '{p}'"),
                );
            }
        }
        _ => {}
    }
}

fn literal(path: &str, options: &[Value], issues: &mut Issues) {
    let terms: Vec<_> = options
        .iter()
        .map(|v| {
            v.as_str()
                .map_or_else(|| v.to_string(), |s| format!("'{s}'"))
        })
        .collect();
    let message = match terms.split_last() {
        Some((last, rest)) if !rest.is_empty() => format!("{} or {last}", rest.join(", ")),
        _ => terms.join(""),
    };
    issue(
        issues,
        path,
        "literal_error",
        &format!("Input should be {message}"),
    );
}

fn length(schema: &Value, n: usize, path: &str, kind: &str, noun: &str, issues: &mut Issues) {
    let (min, max) = if kind == "List" {
        ("minItems", "maxItems")
    } else {
        ("minProperties", "maxProperties")
    };
    for (key, word, code) in [(min, "at least", "too_short"), (max, "at most", "too_long")] {
        if let Some(limit) = schema[key].as_u64()
            && (if key == min {
                n < limit as usize
            } else {
                n > limit as usize
            })
        {
            let plural = if limit == 1 { "" } else { "s" };
            issue(
                issues,
                path,
                code,
                &format!(
                    "{kind} should have {word} {limit} {noun}{plural} after validation, not {n}"
                ),
            );
        }
    }
}

fn object(schema: &Value, value: &mut Value, path: &str, issues: &mut Issues) {
    let props = schema["properties"].as_object();
    let Some(map) = value.as_object_mut() else {
        if props.is_some() {
            issue(
                issues,
                path,
                "model_attributes_type",
                "Input should be a valid dictionary or object to extract fields from",
            );
        } else {
            issue(
                issues,
                path,
                "dict_type",
                "Input should be a valid dictionary",
            );
        }
        return;
    };
    length(schema, map.len(), path, "Dictionary", "item", issues);
    let Some(props) = props else {
        if schema["additionalProperties"].is_object() {
            for (name, item) in map {
                project(
                    &schema["additionalProperties"],
                    item,
                    &child(path, name),
                    issues,
                );
            }
        }
        return;
    };
    for (name, field) in props {
        if let Some(v) = map.get_mut(name) {
            if schema["title"] == "RunRetryCreate"
                && name == "expected_attempt"
                && !(v.is_i64() || v.is_u64())
            {
                issue(
                    issues,
                    &child(path, name),
                    "int_type",
                    "Input should be a valid integer",
                );
            } else if schema["title"] == "McpAccessUpdate" && name == "enabled" && !v.is_boolean() {
                issue(
                    issues,
                    &child(path, name),
                    "bool_type",
                    "Input should be a valid boolean",
                );
            } else {
                project(field, v, &child(path, name), issues);
            }
        } else if schema["required"]
            .as_array()
            .is_some_and(|r| r.iter().any(|k| k == name))
        {
            issue(issues, &child(path, name), "missing", "Field required");
        } else {
            let mut default = field
                .get("default")
                .cloned()
                .unwrap_or_else(|| match field["type"].as_str() {
                    Some("object") => json!({}),
                    Some("array") => json!([]),
                    _ => Value::Null,
                });
            if field.get("$ref").is_some() {
                default = json!({});
                project(field, &mut default, &child(path, name), issues);
            }
            map.insert(name.clone(), default);
        }
    }
    if schema["title"] == "BlueprintRunCreate"
        && map.get("schedule").is_some_and(|s| !s.is_null())
        && map.get("admission_policy") == Some(&json!("enqueue_if_busy"))
        && issues.is_empty()
    {
        issue(
            issues,
            path,
            "value_error",
            "Value error, A scheduled launch cannot also enqueue a run.",
        );
    }
    map.retain(|name, _| {
        if props.contains_key(name) {
            return true;
        }
        if schema["additionalProperties"] == false {
            issue(
                issues,
                &child(path, name),
                "extra_forbidden",
                "Extra inputs are not permitted",
            );
        }
        schema["additionalProperties"] == true
    });
}

fn scalar(schema: &Value, value: &mut Value, path: &str, issues: &mut Issues) {
    let raw = value.as_str().map(str::trim);
    match schema["type"].as_str() {
        Some("boolean") => {
            if value.is_boolean() {
                return;
            }
            let n = value.as_f64();
            let parsed = match raw.unwrap_or("").to_ascii_lowercase().as_str() {
                "false" | "f" | "off" | "no" | "n" | "0" => Some(false),
                "true" | "t" | "on" | "yes" | "y" | "1" => Some(true),
                _ if n == Some(0.) => Some(false),
                _ if n == Some(1.) => Some(true),
                _ => None,
            };
            if let Some(b) = parsed {
                *value = b.into();
            } else if raw.is_some() || n.is_some() {
                issue(
                    issues,
                    path,
                    "bool_parsing",
                    "Input should be a valid boolean, unable to interpret input",
                );
            } else {
                issue(issues, path, "bool_type", "Input should be a valid boolean");
            }
            return;
        }
        Some("integer") if !(value.is_i64() || value.is_u64()) => {
            if let Some(b) = value.as_bool() {
                *value = i64::from(b).into();
            } else if let Some(s) = raw {
                // Pydantic accepts integral decimal strings without rounding.
                let digits = s
                    .split_once('.')
                    .and_then(|(n, f)| (!f.is_empty() && f.bytes().all(|b| b == b'0')).then_some(n))
                    .unwrap_or(s);
                let digits = digits.replace('_', "");
                if let Ok(n) = digits.parse::<i64>() {
                    *value = n.into();
                } else if let Ok(n) = digits.parse::<u64>() {
                    *value = n.into();
                } else {
                    issue(
                        issues,
                        path,
                        "int_parsing",
                        "Input should be a valid integer, unable to parse string as an integer",
                    );
                    return;
                }
            } else if let Some(n) = value.as_f64() {
                if n.fract() != 0. {
                    issue(
                        issues,
                        path,
                        "int_from_float",
                        "Input should be a valid integer, got a number with a fractional part",
                    );
                    return;
                }
                *value = json!(n as i64);
            } else {
                issue(issues, path, "int_type", "Input should be a valid integer");
                return;
            }
        }
        Some("number") if !value.is_number() => {
            if let Some(b) = value.as_bool() {
                *value = json!(i32::from(b) as f64);
            } else if let Some(s) = raw {
                if let Ok(n) = s.parse::<f64>()
                    && n.is_finite()
                {
                    *value = json!(n);
                } else {
                    issue(
                        issues,
                        path,
                        "float_parsing",
                        "Input should be a valid number, unable to parse string as a number",
                    );
                    return;
                }
            } else {
                issue(issues, path, "float_type", "Input should be a valid number");
                return;
            }
        }
        _ => {}
    }
    if let Some(n) = value.as_f64() {
        for (key, code, cmp) in [
            ("minimum", "greater_than_equal", "greater than or equal to"),
            ("maximum", "less_than_equal", "less than or equal to"),
        ] {
            if let Some(limit) = schema[key].as_f64()
                && (if key == "minimum" {
                    n < limit
                } else {
                    n > limit
                })
            {
                issue(
                    issues,
                    path,
                    code,
                    &format!("Input should be {cmp} {limit}"),
                );
            }
        }
    }
}
