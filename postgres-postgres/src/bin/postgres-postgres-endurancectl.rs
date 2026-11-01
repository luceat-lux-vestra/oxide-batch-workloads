use clap::{Parser, Subcommand};
use postgres_postgres::endurance::{cleanup_business, plan_iteration};

/// Campaign #203 bounded control surface for deterministic endurance setup.
///
/// Long-running orchestration and crash/recovery execution are intentionally
/// absent until later campaign slices.
#[derive(Parser)]
#[command(name = "postgres-postgres-endurancectl")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Emit one deterministic logical-iteration plan as JSON.
    Plan {
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
    },
    /// Delete only one iteration's workload-owned destination rows.
    CleanupBusiness {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        import_name: String,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Plan {
            campaign_id,
            sequence,
        } => {
            let plan = plan_iteration(&campaign_id, sequence)?;
            println!("{}", serde_json::to_string_pretty(&plan)?);
            Ok(())
        }
        Command::CleanupBusiness {
            database_url,
            import_name,
        } => {
            let deleted_rows = cleanup_business(&database_url, &import_name).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "import_name": import_name,
                    "deleted_rows": deleted_rows,
                }))?
            );
            Ok(())
        }
    }
}
