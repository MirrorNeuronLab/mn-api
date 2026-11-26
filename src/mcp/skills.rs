use super::{Session, json as content, tools::specs};
use base64::{Engine, engine::general_purpose::STANDARD};
use mn_client_support::{Error, Result, resources};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub(super) async fn resources(s: &Session, method: &str, p: &Value) -> Result<Value> {
    let d = s.supervision("declaration", json!({})).await?;
    let identity = s.supervision("identity", json!({})).await?;
    let bp = identity["blueprint_id"]
        .as_str()
        .ok_or_else(Error::protocol)?;
    let slug: String = bp
        .to_lowercase()
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '-' })
        .collect();
    let slug: String = slug.trim_matches('-').chars().take(64).collect();
    let slug = slug
        .split('-')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    let name = if slug.is_empty() { "co-worker" } else { &slug };
    let base = format!("skill://{name}");
    let description = format!("Supervise the {bp} co-worker through its Job-bound MCP tools.");
    let skill = format!(
        "---\nname: {name}\ndescription: {}\n---\n\n{}",
        content::ascii(&json!(description).to_string()),
        include_str!("supervision.md")
    );
    let mut bundles = vec![(
        base.clone(),
        name.to_owned(),
        description,
        vec![
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
                content::document(specs(&d)?).into_bytes(),
            ),
        ],
    )];
    if d["agent_skills_prepared"] == true {
        let extra = s.supervision("enabled_skill_bundles", json!({})).await?;
        for (n, b) in extra.as_object().ok_or_else(Error::protocol)? {
            let mut files = Vec::new();
            for (path, v) in b["files"].as_object().ok_or_else(Error::protocol)? {
                let data = STANDARD
                    .decode(v["data"].as_str().ok_or_else(Error::protocol)?)
                    .map_err(|_| Error::protocol())?;
                if v["sha256"] != format!("{:x}", Sha256::digest(&data)) {
                    return Err(Error::protocol());
                }
                files.push((path.clone(), data));
            }
            let description = files
                .iter()
                .find(|(path, _)| path == "SKILL.md")
                .and_then(|(_, bytes)| std::str::from_utf8(bytes).ok())
                .and_then(|doc| doc.split("---").nth(1))
                .ok_or_else(Error::protocol)?
                .lines()
                .find_map(|line| line.strip_prefix("description:").map(str::trim))
                .unwrap_or(n)
                .to_string();
            bundles.push((
                format!("skill://dependencies/{n}"),
                n.clone(),
                description,
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
            let mime = mime_guess::from_path(&path)
                .first_or_octet_stream()
                .to_string();
            refs.push(json!({"uri":uri,"digest":format!("sha256:{:x}",Sha256::digest(&bytes)),"size":bytes.len()}));
            let mut resource = json!({"uri":uri,"name":uri.rsplit('/').next(),"mimeType":mime});
            if path == "SKILL.md" {
                resource["name"] = json!(name);
                resource["description"] = json!(desc);
            }
            resources.push(resource);
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
    let value = match method {
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
            let mut children = BTreeMap::new();
            for row in resources {
                let uri = row["uri"].as_str().ok_or_else(Error::protocol)?;
                if let Some(relative) = uri.strip_prefix(&prefix) {
                    if let Some((dir, _)) = relative.split_once('/') {
                        children.insert(dir.to_owned(), json!({"uri":format!("{prefix}{dir}"),"name":dir,"mimeType":"inode/directory"}));
                    } else {
                        children.insert(relative.to_owned(), row);
                    }
                }
            }
            if children.is_empty() {
                return Err(bad());
            }
            json!({"resources":children.into_values().collect::<Vec<_>>()})
        }
        _ => json!({"resources":resources}),
    };
    Ok(value)
}
