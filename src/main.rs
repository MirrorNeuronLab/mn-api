#[cfg_attr(target_os = "wasi", tokio::main(flavor = "current_thread"))]
#[cfg_attr(not(target_os = "wasi"), tokio::main)]
async fn main() -> std::process::ExitCode {
    if let Err(e) = mn_api::run().await {
        eprintln!("mn-api: {e}");
        return std::process::ExitCode::FAILURE;
    }
    std::process::ExitCode::SUCCESS
}
