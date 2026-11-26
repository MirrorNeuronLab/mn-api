#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    mn_api::web::run().await
}
