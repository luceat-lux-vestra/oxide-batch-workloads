use std::path::PathBuf;

use clap::{Parser, Subcommand};
use postgres_postgres::endurance::crash::execute_crash_iteration;
use postgres_postgres::endurance::{cleanup_business, execute_clean_iteration, plan_iteration};

/// Campaign #203 bounded control surface for deterministic endurance setup,
/// one clean logical iteration, and one scheduled crash/recovery iteration.
///
/// Long-running loops, sampling, evidence persistence, and resume state are
/// intentionally absent until later campaign slices.
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
    /// Execute one clean logical iteration through the real workload binary,
    /// independently verify it, then clean only that iteration's business rows.
    RunClean {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
    },
    /// Execute one scheduled crash/recovery logical iteration through real
    /// workload, recovery, continuation, and verification child processes.
    RunCrash {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        campaign_id: String,
        #[arg(long)]
        sequence: u64,
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
        Command::RunClean {
            database_url,
            campaign_id,
            sequence,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result =
                execute_clean_iteration(&workload_binary, &database_url, &campaign_id, sequence)
                    .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::RunCrash {
            database_url,
            campaign_id,
            sequence,
        } => {
            let workload_binary = sibling_workload_binary()?;
            let result =
                execute_crash_iteration(&workload_binary, &database_url, &campaign_id, sequence)
                    .await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
    }
}

fn sibling_workload_binary() -> anyhow::Result<PathBuf> {
    let current_exe = std::env::current_exe()?;
    let parent = current_exe.parent().ok_or_else(|| {
        anyhow::anyhow!(
            "cannot resolve postgres-postgres sibling directory from {}",
            current_exe.display()
        )
    })?;
    let binary_name = if cfg!(windows) {
        "postgres-postgres.exe"
    } else {
        "postgres-postgres"
    };
    Ok(parent.join(binary_name))
}
