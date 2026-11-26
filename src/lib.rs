//! MirrorNeuron REST and web UI servers.
mod app;
mod auth;
mod catalog;
mod config;
mod error;
mod grants;
mod http;
mod mcp;
mod oauth;
mod schema;
mod ui;
mod uploads;
pub mod web;

pub use app::{router, run};
pub use config::Config;
