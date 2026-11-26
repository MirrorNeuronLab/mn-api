//! The SQLite layout also opens connection policies and grants from Python installs.
use super::*;
use rusqlite::{OptionalExtension, params};
use sha2::{Digest, Sha256};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::time::{SystemTime, UNIX_EPOCH};
pub fn digest(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn secret() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}
pub struct Store(rusqlite::Connection);
type DbResult<T> = rusqlite::Result<T>;
fn db<T>(v: DbResult<T>) -> Result<T> {
    v.map_err(|_| {
        Error::new(
            503,
            "MN_MCP_STORE_UNAVAILABLE",
            "Cannot access the MCP connection store.",
        )
    })
}
impl Store {
    pub fn open(root: &Path) -> Result<Self> {
        let dir = root.join("mcp_connections");
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&dir)
            .map_err(|_| Error::storage())?;
        let path = dir.join("connections.sqlite3");
        std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(&path)
            .map_err(|_| Error::storage())?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .map_err(|_| Error::storage())?;
        let conn = db(rusqlite::Connection::open(path))?;
        db(conn.execute_batch("CREATE TABLE IF NOT EXISTS policies (job_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, generation INTEGER NOT NULL, key_hash TEXT, server_url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (key_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, job_id TEXT, generation INTEGER, expires REAL NOT NULL, data TEXT NOT NULL);"))?;
        Ok(Self(conn))
    }
    pub fn policy(&self, job: &str) -> Result<Value> {
        db(self.0.query_row("SELECT enabled,generation,key_hash,server_url FROM policies WHERE job_id=?1",[job],|r|Ok(json!({"job_id":job,"enabled":r.get::<_,bool>(0)?,"generation":r.get::<_,i64>(1)?,"key_hash":r.get::<_,Option<String>>(2)?,"server_url":r.get::<_,String>(3)?}))).optional()).map(|p|p.unwrap_or_else(||json!({"job_id":job,"enabled":false,"generation":0,"server_url":""})))
    }
    pub fn resource(&self, url: &str) -> Result<Value> {
        let job = db(self
            .0
            .query_row(
                "SELECT job_id FROM policies WHERE enabled=1 AND server_url=?1",
                [url],
                |r| r.get::<_, String>(0),
            )
            .optional())?;
        if let Some(job) = job {
            self.policy(&job)
        } else {
            Ok(Value::Null)
        }
    }
    pub fn configure(&mut self, job: &str, enabled: bool, url: &str) -> Result<Option<String>> {
        let key = enabled.then(secret);
        let tx = db(self.0.transaction())?;
        db(tx.execute("INSERT INTO policies VALUES (?1,?2,1,?3,?4) ON CONFLICT(job_id) DO UPDATE SET enabled=excluded.enabled,generation=generation+1,key_hash=excluded.key_hash,server_url=excluded.server_url",params![job,enabled,key.as_deref().map(digest),url]))?;
        db(tx.execute(
            "DELETE FROM grants WHERE job_id=?1 AND kind!='internal'",
            [job],
        ))?;
        db(tx.commit())?;
        Ok(key)
    }
    pub fn issue(
        &self,
        kind: &str,
        job: Option<&str>,
        generation: i64,
        ttl: u64,
        data: &Value,
    ) -> Result<String> {
        let key = secret();
        let tx = db(self.0.unchecked_transaction())?;
        db(tx.execute("DELETE FROM grants WHERE expires<?1", [now()]))?;
        db(tx.execute(
            "INSERT INTO grants VALUES (?1,?2,?3,?4,?5,?6)",
            params![
                digest(&key),
                kind,
                job,
                generation,
                now() + ttl as f64,
                data.to_string()
            ],
        ))?;
        db(tx.commit())?;
        Ok(key)
    }
    pub fn grant(&self, key: &str, kind: Option<&str>, consume: bool) -> Result<Option<Value>> {
        if !(16..=256).contains(&key.len()) {
            return Ok(None);
        }
        let sql = "SELECT kind,job_id,generation,data FROM grants g WHERE key_hash=?1 AND expires>?2 AND (?3 IS NULL OR kind=?3) AND (job_id IS NULL OR kind='internal' OR EXISTS (SELECT 1 FROM policies p WHERE p.job_id=g.job_id AND p.enabled=1 AND p.generation=g.generation))";
        let row = db(self
            .0
            .query_row(sql, params![digest(key), now(), kind], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, String>(3)?,
                ))
            })
            .optional())?;
        let Some((kind, job, generation, data)) = row else {
            return Ok(None);
        };
        if consume
            && db(self
                .0
                .execute("DELETE FROM grants WHERE key_hash=?1", [digest(key)]))?
                != 1
        {
            return Ok(None);
        }
        Ok(Some(
            json!({"kind":kind,"job_id":job,"generation":generation,"data":serde_json::from_str::<Value>(&data).map_err(|_|Error::storage())?}),
        ))
    }
    pub fn revoke(&self, k: &str) -> Result<()> {
        db(self.0.execute(
            "DELETE FROM grants WHERE key_hash=?1 AND kind IN ('access','refresh')",
            [digest(k)],
        ))?;
        Ok(())
    }
    pub fn authority(&self, job: &str, key: &str) -> Result<Option<bool>> {
        if let Some(g) = self.grant(key, None, false)?
            && g["job_id"] == job
        {
            if g["kind"] == "internal" {
                return Ok(Some(true));
            }
            if g["kind"] == "access"
                && g["data"]["scopes"]
                    .as_array()
                    .is_some_and(|s| s.iter().any(|v| v == "co_worker:supervise"))
            {
                return Ok(Some(false));
            }
        }
        let p = self.policy(job)?;
        Ok((p["enabled"] == true
            && p["key_hash"]
                .as_str()
                .is_some_and(|h| equal(&digest(key), h)))
        .then_some(false))
    }
}
