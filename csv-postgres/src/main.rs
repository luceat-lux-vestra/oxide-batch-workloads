mod failpoint;
mod generator;
mod job;
mod processor;
mod verify;
mod writer;

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use generator::Profile;

/// OxideBatch 0.6.0 real-workload validation: streaming CSV -> PostgreSQL
/// restartable batch import.
#[derive(Parser)]
#[command(name = "csv-postgres")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Generate a deterministic synthetic CSV dataset.
    Generate {
        #[arg(long, default_value = "customers.csv")]
        output: PathBuf,
        #[arg(long)]
        rows: Option<u64>,
        #[arg(long, default_value_t = 42)]
        seed: u64,
        #[arg(long, default_value = "normal")]
        profile: Profile,
        /// 1-based row index to duplicate the preceding row's customer_id at.
        #[arg(long)]
        inject_duplicate_at: Option<u64>,
        /// 1-based row index to write with a missing trailing field.
        #[arg(long)]
        inject_malformed_at: Option<u64>,
        /// 1-based row index to write with a non-numeric amount field.
        #[arg(long)]
        inject_bad_amount_at: Option<u64>,
    },
    /// Run OxideBatch's own PostgreSQL metadata migrations plus this
    /// workload's business-table migration.
    Migrate {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
    /// Launch (or resume) the CSV import job through the real production
    /// launch path (`oxide_batch::JobLauncher`).
    Run {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        input: PathBuf,
        /// Logical import name; part of job identity alongside the input's
        /// SHA-256 (see job::input_identity).
        #[arg(long, default_value = "customers_import")]
        import_name: String,
        #[arg(long, default_value_t = 500)]
        chunk_size: u32,
        /// e.g. "chunk:5" or "row:2501".
        #[arg(long)]
        fail_at: Option<String>,
        #[arg(long, default_value = "before-write")]
        failure_mode: failpoint::FailureMode,
        /// Abort the process (std::process::abort) at the failpoint instead
        /// of returning a typed graceful error.
        #[arg(long, default_value_t = false)]
        hard_crash: bool,
        /// Use `ON CONFLICT (customer_id) DO NOTHING` instead of a strict
        /// insert (see spec ss26: strict vs. idempotent write scenarios).
        #[arg(long, default_value_t = false)]
        idempotent_writes: bool,
    },
    /// Mark a `Starting/Started/Stopping/Unknown` execution left behind by a
    /// hard crash as recoverable, so a subsequent `run` can resume it.
    Recover {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        import_name: String,
        #[arg(long)]
        input: PathBuf,
    },
    /// Query PostgreSQL directly (never log strings) to verify the business
    /// table's contents against the source CSV.
    Verify {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        input: PathBuf,
    },
    /// Drop and recreate the business table (never touches OxideBatch's own
    /// `oxide_batch` schema).
    Reset {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let cli = Cli::parse();
    match cli.command {
        Command::Generate {
            output,
            rows,
            seed,
            profile,
            inject_duplicate_at,
            inject_malformed_at,
            inject_bad_amount_at,
        } => {
            let rows = rows.unwrap_or_else(|| profile.default_rows());
            let manifest = generator::generate(
                &output,
                rows,
                seed,
                inject_duplicate_at,
                inject_malformed_at,
                inject_bad_amount_at,
            )?;
            let sidecar = output.with_extension("manifest.json");
            std::fs::write(&sidecar, serde_json::to_string_pretty(&manifest)?)?;
            println!(
                "generated {} {} rows (seed {}) -> {} ({} bytes, sha256 {})",
                profile.as_str(),
                manifest.rows,
                manifest.seed,
                output.display(),
                manifest.file_size_bytes,
                manifest.sha256,
            );
            Ok(())
        }
        Command::Migrate { database_url } => job::migrate(&database_url).await,
        Command::Run {
            database_url,
            input,
            import_name,
            chunk_size,
            fail_at,
            failure_mode,
            hard_crash,
            idempotent_writes,
        } => {
            let fail_at = fail_at.as_deref().map(failpoint::FailAt::parse).transpose()?;
            job::run(
                &database_url,
                &input,
                &import_name,
                chunk_size,
                fail_at,
                failure_mode,
                hard_crash,
                idempotent_writes,
            )
            .await
        }
        Command::Recover {
            database_url,
            import_name,
            input,
        } => job::recover(&database_url, &import_name, &input).await,
        Command::Verify { database_url, input } => verify::verify(&database_url, &input).await,
        Command::Reset { database_url } => job::reset(&database_url).await,
    }
}
