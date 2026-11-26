#[tokio::main]
async fn main() -> std::process::ExitCode {
    if let Err(e) = mn_api::run().await {
        eprintln!("mn-api: {e}");
        return std::process::ExitCode::FAILURE;
    }
    std::process::ExitCode::SUCCESS
}
