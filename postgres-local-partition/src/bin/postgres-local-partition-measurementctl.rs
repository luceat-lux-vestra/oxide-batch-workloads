use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[path = "../measurement_sessions.rs"]
mod measurement_sessions;
#[path = "../measurement_timings.rs"]
mod measurement_timings;

#[derive(Parser)]
#[command(name = "postgres-local-partition-measurementctl")]
#[command(about = "Measurement-only public inspection/observer tool for campaign #93")]
struct Cli {
    #[arg(long, env = "DATABASE_URL")]
    database_url: String,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    InspectTimings {
        #[arg(long)]
        run_name: String,
    },
    ObserveSessions {
        #[arg(long)]
        output: PathBuf,
        #[arg(long)]
        ready_file: PathBuf,
        #[arg(long)]
        stop_file: PathBuf,
        #[arg(long, default_value_t = 10)]
        interval_ms: u64,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::InspectTimings { run_name } => {
            measurement_timings::inspect(&cli.database_url, &run_name).await?
        }
        Command::ObserveSessions {
            output,
            ready_file,
            stop_file,
            interval_ms,
        } => {
            measurement_sessions::observe(
                &cli.database_url,
                &output,
                &ready_file,
                &stop_file,
                interval_ms,
            )
            .await?
        }
    }
    Ok(())
}
