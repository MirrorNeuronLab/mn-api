use crate::{
    config::{Config, Cursor},
    http::json_response,
};
use axum::response::Response;
use mn_client_support::{Error, Result};
use serde_json::{Value, json};
use std::{collections::HashMap, time::Instant};
use uuid::Uuid;
pub(crate) async fn list_blueprints(c: &Config, q: Value) -> Result<Response> {
    let cat = q["category"].as_str();
    let list = c.ipc.catalog(None, cat).await?;
    let rows = list.as_array().ok_or_else(Error::protocol)?;
    let page = q["page_size"].as_u64().unwrap_or(50) as usize;
    let mut registry = c.cursors.lock().unwrap();
    let now = Instant::now();
    registry.retain(|_, p| p.expires > now);
    let (ids, offset) = if let Some(t) = q["page_token"].as_str() {
        let p = registry
            .get(t)
            .filter(|p| p.category.as_deref() == cat)
            .ok_or_else(|| {
                Error::new(400, "bad_request", "The page token is invalid or expired.")
            })?;
        (p.ids.clone(), p.offset)
    } else {
        (
            rows.iter()
                .filter_map(|r| r["id"].as_str().map(str::to_owned))
                .collect(),
            0,
        )
    };
    let items: HashMap<_, _> = rows
        .iter()
        .filter_map(|r| r["id"].as_str().map(|id| (id, r)))
        .collect();
    let mut batch = Vec::new();
    let mut pos = offset;
    while pos < ids.len() && batch.len() < page {
        if let Some(r) = items.get(ids[pos].as_str()) {
            batch.push(*r);
        }
        pos += 1;
    }
    let next = if pos < ids.len() {
        if registry.len() >= 10000
            && let Some(old) = registry
                .iter()
                .min_by_key(|(_, p)| p.expires)
                .map(|(k, _)| k.clone())
        {
            registry.remove(&old);
        }
        let token = Uuid::new_v4().to_string();
        registry.insert(
            token.clone(),
            Cursor {
                category: cat.map(str::to_owned),
                ids,
                offset: pos,
                expires: now + std::time::Duration::from_secs(86400),
            },
        );
        Some(token)
    } else {
        None
    };
    json_response(200, json!({"items":batch,"next_page_token":next}))
}
