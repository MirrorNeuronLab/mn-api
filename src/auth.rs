use crate::config::Config;
use axum::http::HeaderMap;
use mn_client_support::Error;
use subtle::ConstantTimeEq;
pub(crate) fn header_str<'a>(h: &'a HeaderMap, k: &str) -> &'a str {
    h.get(k).and_then(|h| h.to_str().ok()).unwrap_or_default()
}
pub(crate) fn bearer(h: &HeaderMap) -> &str {
    header_str(h, "authorization")
        .split_once(' ')
        .filter(|(s, _)| s.eq_ignore_ascii_case("bearer"))
        .map(|(_, s)| s)
        .unwrap_or_default()
}
pub(crate) fn equal(a: &str, b: &str) -> bool {
    a.as_bytes().ct_eq(b.as_bytes()).into()
}
pub(crate) fn owner(c: &Config, h: &HeaderMap) -> bool {
    !c.token.is_empty() && equal(bearer(h), &c.token)
}
pub(crate) fn unauthorized() -> Error {
    Error::new(401, "unauthorized", "Missing or invalid bearer token.")
}
