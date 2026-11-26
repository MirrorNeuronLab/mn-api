use crate::auth::header_str;
use axum::body::Body;
use axum::extract::Request;
use futures_util::{StreamExt, TryStreamExt};
use mn_client_support::{Error, Result, control::Chunks};
pub(crate) fn too_large() -> Error {
    Error::new(
        413,
        "request_too_large",
        "The request exceeds the configured size limit.",
    )
}
pub(crate) async fn upload_body(
    action: &str,
    r: Request,
    limit: usize,
    file_limit: usize,
) -> Result<(Chunks, Option<String>)> {
    use http_body_util::Limited;
    let ct = header_str(r.headers(), "content-type").to_owned();
    let raw = Body::new(Limited::new(r.into_body(), limit))
        .into_data_stream()
        .map_err(|_| too_large());
    if action == "restore_job_backup" {
        if ct.split(';').next() != Some("application/zip") {
            return Err(Error::new(
                415,
                "unsupported_media_type",
                "Send the backup with Content-Type application/zip.",
            ));
        }
        return Ok((Box::pin(raw), None));
    }
    let boundary = multer::parse_boundary(&ct)
        .map_err(|_| Error::invalid("Expected a multipart bundle upload."))?;
    let mut multipart = multer::Multipart::new(raw, boundary);
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| Error::invalid("Invalid multipart upload."))?
    {
        if field.name() == Some("bundle") {
            let name = field.file_name().map(str::to_owned);
            let mut size = 0;
            let chunks = field.map(move |item| {
                let b = item.map_err(|_| Error::invalid("Interrupted multipart upload."))?;
                size += b.len();
                if size > file_limit {
                    Err(too_large())
                } else {
                    Ok(b)
                }
            });
            return Ok((Box::pin(chunks), name));
        }
    }
    Err(Error::invalid("Missing bundle file."))
}
