use anyhow::{bail, Context, Result};
use clap::{CommandFactory, Parser, Subcommand};
use sqlx::postgres::PgPoolOptions;

const CONTRACT_VERSION: &str = "payment-recovery-v1";
const OXIDE_BATCH_VERSION: &str = "0.7.0";

#[derive(Debug, Parser)]
#[command(
    name = "payment-recovery-postgres",
    about = "OxideBatch payment recovery workload for issue #271"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Create workload-owned recovery schemas and tables.
    Migrate {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
    /// Freeze eligible Merchant UNKNOWN/RETRY_PENDING candidates for one logical run.
    SnapshotCandidates {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "MERCHANT_DATABASE_URL")]
        merchant_database_url: String,
        #[arg(long)]
        run_id: String,
        #[arg(long)]
        business_date: String,
        #[arg(long)]
        eligibility_cutoff: String,
        #[arg(long)]
        scenario_set_id: String,
        #[arg(long, default_value = "payment-recovery-policy-v1")]
        policy_version: String,
    },
    /// Execute payment recovery processing for one frozen run.
    Run {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "MERCHANT_BASE_URL")]
        merchant_base_url: String,
        #[arg(long, env = "PROVIDER_BASE_URL")]
        provider_base_url: String,
        #[arg(long)]
        run_id: String,
    },
    /// Continue an interrupted OxideBatch execution using public recovery APIs.
    Recover {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        run_id: String,
    },
    /// Independently verify recovery classification and side-effect cardinality.
    Verify {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, env = "PROVIDER_BASE_URL")]
        provider_base_url: String,
        #[arg(long)]
        run_id: String,
    },
    /// Clear only workload-owned recovery state.
    Reset {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    Cli::command().debug_assert();

    match Cli::parse().command {
        Command::Migrate { database_url } => migrate(&database_url).await,
        Command::SnapshotCandidates { .. } => checkpoint_four("snapshot-candidates"),
        Command::Run { .. } => checkpoint_five("run"),
        Command::Recover { .. } => checkpoint_six("recover"),
        Command::Verify { .. } => checkpoint_seven("verify"),
        Command::Reset { .. } => checkpoint_seven("reset"),
    }
}

async fn migrate(database_url: &str) -> Result<()> {
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(database_url)
        .await
        .context("connect payment recovery PostgreSQL")?;

    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .context("apply payment recovery migrations")?;

    pool.close().await;
    Ok(())
}

fn checkpoint_four(command: &str) -> Result<()> {
    bail!("{command} is intentionally deferred until issue #271 checkpoint 4")
}

fn checkpoint_five(command: &str) -> Result<()> {
    bail!("{command} is intentionally deferred until issue #271 checkpoint 5")
}

fn checkpoint_six(command: &str) -> Result<()> {
    bail!("{command} is intentionally deferred until issue #271 checkpoint 6")
}

fn checkpoint_seven(command: &str) -> Result<()> {
    bail!("{command} is intentionally deferred until issue #271 checkpoint 7")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_contract_is_well_formed() {
        Cli::command().debug_assert();
    }

    #[test]
    fn cli_exposes_frozen_checkpoint_commands() {
        let names: Vec<_> = Cli::command()
            .get_subcommands()
            .map(|command| command.get_name())
            .collect();

        assert_eq!(
            names,
            [
                "migrate",
                "snapshot-candidates",
                "run",
                "recover",
                "verify",
                "reset",
            ]
        );
    }

    #[test]
    fn release_and_contract_identity_are_frozen() {
        assert_eq!(CONTRACT_VERSION, "payment-recovery-v1");
        assert_eq!(OXIDE_BATCH_VERSION, "0.7.0");
    }
}
