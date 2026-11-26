use clap::Parser;
use mn_client_support::adapter::Adapter;
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Instant,
};
#[derive(Parser)]
#[command(version = mn_client_support::release_version(env!("CARGO_PKG_VERSION")), about = "MirrorNeuron REST API")]
pub(crate) struct Options {
    #[arg(long, env = "MN_API_HOST", default_value = "localhost")]
    pub(crate) host: String,
    #[arg(long, env = "MN_API_PORT", default_value_t = 54001)]
    pub(crate) port: u16,
    #[arg(long,env="MN_API_REQUEST_SIZE_LIMIT_BYTES",default_value_t=5*1024*1024)]
    pub(crate) request_limit: usize,
    #[arg(long,env="MN_API_BLUEPRINT_UPLOAD_LIMIT_BYTES",default_value_t=32*1024*1024*1024)]
    pub(crate) upload_limit: u64,
}
pub(crate) struct Cursor {
    pub(crate) category: Option<String>,
    pub(crate) ids: Vec<String>,
    pub(crate) offset: usize,
    pub(crate) expires: Instant,
}
pub struct Config {
    pub(crate) ipc: Adapter,
    pub(crate) cors: Option<crate::http::cors::Cors>,
    pub(crate) home: PathBuf,
    pub(crate) token: String,
    pub(crate) limit: usize,
    pub(crate) upload: u64,
    pub(crate) cursors: Mutex<HashMap<String, Cursor>>,
}
pub(crate) type Shared = Arc<Config>;

impl Config {
    /// Configure a REST server for an explicitly selected client home and owner token.
    pub fn new(home: PathBuf, token: String) -> Self {
        Self {
            ipc: Adapter::new(&home),
            cors: None,
            home,
            token,
            limit: 5 * 1024 * 1024,
            upload: 32 * 1024 * 1024 * 1024,
            cursors: Mutex::default(),
        }
    }
    pub fn with_cors_origins(mut self, origins: &str) -> mn_client_support::Result<Self> {
        let origins = origins
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| {
                s.parse()
                    .map_err(|_| mn_client_support::Error::invalid("Invalid CORS origin."))
            })
            .collect::<mn_client_support::Result<Vec<_>>>()?;
        if !origins.is_empty() {
            self.cors = Some(crate::http::cors::Cors::new(origins));
        }
        Ok(self)
    }
}
