use std::path::PathBuf;

use clap::{Parser, Subcommand};
use postgres_postgres::failpoint::FailureMode;
use postgres_postgres::job::ReaderMode;
use postgres_postgres::{generator, job, verify};

/// OxideBatch 0.6.0 real-workload validation: PostgreSQL -> PostgreSQL
/// restartable transform, in either cursor mode (a real streamed
/// server-side cursor) or paging mode (independent, bounded keyset pages).
#[derive(Parser)]
#[command(name = "postgres-postgres")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run OxideBatch's own PostgreSQL metadata migrations plus this
    /// workload's `app_source`/`app_business` migration.
    Migrate {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
    /// Generate a deterministic synthetic dataset directly into
    /// `app_source.source_customer`.
    Seed {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, default_value_t = 1_000)]
        rows: u64,
        #[arg(long, default_value_t = 42)]
        seed: u64,
        /// Added to every generated `customer_id`, so independent test runs
        /// sharing one database never collide on the primary key.
        #[arg(long, default_value_t = 0)]
        id_offset: u64,
    },
    /// Launch (or resume) the transform job through the real production
    /// launch path (`oxide_batch::JobLauncher`), in the reader mode
    /// `--reader` explicitly selects. Reader mode is part of job identity
    /// (see job::run) alongside the import name and source digest.
    Run {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        /// Logical import name; part of job identity alongside the current
        /// source content's digest and the selected reader mode (see
        /// job::run).
        #[arg(long, default_value = "customers_transform")]
        import_name: String,
        #[arg(long, default_value_t = 500)]
        chunk_size: u32,
        /// Required: which released OxideBatch reader component performs
        /// this run. No default -- an unspecified reader mode is a usage
        /// error, not a silently chosen one.
        #[arg(long)]
        reader: ReaderMode,
        /// Cursor-mode only: how many rows one server-side `FETCH` round
        /// trip retrieves; bounds this run's reader memory to
        /// O(fetch_size). Defaults to `job::DEFAULT_FETCH_SIZE` when
        /// omitted under `--reader cursor`. Rejected under `--reader
        /// paging`.
        #[arg(long)]
        fetch_size: Option<usize>,
        /// Paging-mode only: how many rows one bounded keyset page
        /// retrieves; no server-side resource is held between pages, and
        /// no `OFFSET` is ever issued (the released
        /// `postgres_paging_reader`'s own contract). Defaults to
        /// `job::DEFAULT_PAGE_SIZE` when omitted under `--reader paging`.
        /// Rejected under `--reader cursor`.
        #[arg(long)]
        page_size: Option<usize>,
        /// Deterministic fault injection (campaign #63 PR 3): 1-based
        /// ordinal of the chunk transaction attempt to target. Omitted (or
        /// `0`) means no fault is ever injected -- a clean run, unchanged
        /// from PR 1/PR 2 behavior.
        #[arg(long, default_value_t = 0)]
        fail_at_chunk: u32,
        /// Which point in the targeted chunk's lifecycle to fire at. Only
        /// meaningful when `--fail-at-chunk` is nonzero.
        #[arg(long, default_value = "during-write")]
        failure_mode: FailureMode,
        /// Instead of returning a typed graceful error at the failpoint,
        /// write this process's PID to the given marker file and then
        /// block forever, so a test harness can observe the marker and
        /// deliver a real SIGKILL at the exact semantic boundary under
        /// test. Only meaningful together with `--fail-at-chunk`.
        #[arg(long)]
        pause_for_kill: Option<PathBuf>,
    },
    /// Marks a `Starting`/`Started`/`Stopping`/`Unknown` execution left
    /// behind by a hard crash as recoverable, through the public
    /// OxideBatch operator/recovery API only (never by mutating
    /// `oxide_batch` metadata directly), so a subsequent `run` can resume
    /// it. Which execution is recovered is fully deterministic: the exact
    /// same `(import_name, source_digest, reader_mode)` identity `run`
    /// itself would resolve right now, with `source_digest` recomputed live
    /// from the current source content -- never a caller-supplied digest,
    /// never an ambiguous "most recent" execution across identities. See
    /// `job::recover`'s doc comment.
    Recover {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long)]
        import_name: String,
        #[arg(long)]
        reader: ReaderMode,
    },
    /// Independently verify `app_business.customer_projection` against
    /// `app_source.source_customer` for one import name's current source
    /// identity. Never calls into the OxideBatch execution path.
    Verify {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
        #[arg(long, default_value = "customers_transform")]
        import_name: String,
    },
    /// Truncates the workload-owned `app_source`/`app_business` tables
    /// (never drops them, never touches OxideBatch's own `oxide_batch`
    /// schema).
    Reset {
        #[arg(long, env = "DATABASE_URL")]
        database_url: String,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> anyhow::Result<()> {
    // Defaults to info (per-job/per-step/per-chunk progress, never
    // per-row) so useful diagnostics are visible without an operator
    // needing to know to set RUST_LOG first; still fully overridable.
    // Written to stderr so it never interleaves with `verify`'s JSON report
    // on stdout.
    let env_filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .with_ansi(false)
        .with_writer(std::io::stderr)
        .init();

    let cli = Cli::parse();
    match cli.command {
        Command::Migrate { database_url } => job::migrate(&database_url).await,
        Command::Seed {
            database_url,
            rows,
            seed,
            id_offset,
        } => {
            let pool = sqlx::postgres::PgPoolOptions::new()
                .connect(&database_url)
                .await?;
            generator::seed(&pool, rows, seed, id_offset).await?;
            pool.close().await;
            println!("seeded {rows} rows (seed {seed}, id_offset {id_offset})");
            Ok(())
        }
        Command::Run {
            database_url,
            import_name,
            chunk_size,
            reader,
            fetch_size,
            page_size,
            fail_at_chunk,
            failure_mode,
            pause_for_kill,
        } => {
            job::run(
                &database_url,
                &import_name,
                chunk_size,
                reader,
                fetch_size,
                page_size,
                fail_at_chunk,
                failure_mode,
                pause_for_kill,
            )
            .await
        }
        Command::Recover {
            database_url,
            import_name,
            reader,
        } => job::recover(&database_url, &import_name, reader).await,
        Command::Verify {
            database_url,
            import_name,
        } => verify::verify(&database_url, &import_name).await,
        Command::Reset { database_url } => job::reset(&database_url).await,
    }
}
