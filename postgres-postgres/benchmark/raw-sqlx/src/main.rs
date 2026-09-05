use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use clap::{Args, Parser, Subcommand, ValueEnum};
use futures_util::TryStreamExt;
use sha2::{Digest, Sha256};
use sqlx::{AssertSqlSafe, PgPool, Postgres, QueryBuilder, Row, Transaction};

const PAGING_DEFINITION_REVISION: &str = "raw-sqlx-postgres-postgres-paging-v1";
const CURSOR_DEFINITION_REVISION: &str = "raw-sqlx-postgres-postgres-cursor-v1";
const CURSOR_NAME: &str = "raw_sqlx_customer_cursor";
const DEFAULT_FETCH_SIZE: usize = 500;
const DEFAULT_PAGE_SIZE: usize = 750;
const COLUMNS_PER_ROW: usize = 7;
const MAX_PARAMETERS_PER_STATEMENT: usize = 2_000;
const ROWS_PER_STATEMENT: usize = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW;
const MAX_BOUND_PARAMETERS: usize = ROWS_PER_STATEMENT * COLUMNS_PER_ROW;
const MAX_CHUNK_SIZE: usize = 1_000_000;
const MAX_READ_BATCH_SIZE: usize = 1_000_000;
const FINGERPRINT_LEN: usize = 16;
const PREMIUM_THRESHOLD_CENTS: i64 = 50_000;

#[derive(Parser)]
#[command(name = "raw-sqlx")]
struct Cli {
    #[arg(long, env = "DATABASE_URL")]
    database_url: String,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Migrate,
    Run(RunArgs),
    Inspect {
        #[arg(long)]
        import_name: String,
    },
}

#[derive(Args)]
struct RunArgs {
    #[arg(long)]
    import_name: String,
    #[arg(long, value_enum)]
    reader: ReaderMode,
    #[arg(long, default_value_t = 1_000)]
    chunk_size: usize,
    #[arg(long)]
    fetch_size: Option<usize>,
    #[arg(long)]
    page_size: Option<usize>,
    #[arg(long)]
    fail_after_chunk: Option<u64>,
    #[arg(long)]
    pause_at_chunk: Option<u64>,
    #[arg(long, value_enum)]
    pause_phase: Option<PausePhase>,
    #[arg(long)]
    pause_marker: Option<PathBuf>,
}

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
enum ReaderMode {
    Cursor,
    Paging,
}

impl ReaderMode {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Cursor => "cursor",
            Self::Paging => "paging",
        }
    }

    const fn definition_revision(self) -> &'static str {
        match self {
            Self::Cursor => CURSOR_DEFINITION_REVISION,
            Self::Paging => PAGING_DEFINITION_REVISION,
        }
    }
}

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
enum PausePhase {
    BeforeCommit,
    AfterCommit,
}

impl PausePhase {
    const fn as_str(self) -> &'static str {
        match self {
            Self::BeforeCommit => "before-commit",
            Self::AfterCommit => "after-commit",
        }
    }
}

#[derive(Debug, Clone)]
struct KillPause {
    chunk: u64,
    phase: PausePhase,
    marker: PathBuf,
}

#[derive(Debug, Clone)]
struct RunConfig {
    reader_mode: ReaderMode,
    chunk_size: usize,
    read_batch_size: usize,
    fail_after_chunk: Option<u64>,
    kill_pause: Option<KillPause>,
}

#[derive(Debug, Clone, Copy)]
struct ExecutionIdentity<'a> {
    import_name: &'a str,
    source_digest: &'a str,
    reader_mode: ReaderMode,
}

impl ExecutionIdentity<'_> {
    const fn definition_revision(self) -> &'static str {
        self.reader_mode.definition_revision()
    }
}

#[derive(Debug)]
struct SourceRow {
    customer_id: i64,
    full_name: String,
    is_active: bool,
    balance_cents: i64,
}

#[derive(Debug)]
struct ProjectedRow {
    import_name: String,
    source_digest: String,
    customer_id: i64,
    display_name: String,
    loyalty_score: i64,
    is_premium: bool,
    row_fingerprint: [u8; FINGERPRINT_LEN],
}

#[derive(Debug, Clone, Copy)]
struct CheckpointUpdate {
    last_customer_id: i64,
    committed_chunks: u64,
    committed_rows: u64,
}

#[derive(Debug)]
struct RunState {
    durable_position: Option<i64>,
    committed_chunks: u64,
    committed_rows: u64,
    chunk: Vec<ProjectedRow>,
}

impl RunState {
    fn new(chunk_size: usize, checkpoint: (Option<i64>, u64, u64)) -> Self {
        Self {
            durable_position: checkpoint.0,
            committed_chunks: checkpoint.1,
            committed_rows: checkpoint.2,
            chunk: Vec::with_capacity(chunk_size),
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let pool = PgPool::connect(&cli.database_url)
        .await
        .context("connect to PostgreSQL")?;

    match cli.command {
        Command::Migrate => migrate(&pool).await,
        Command::Run(args) => run(&pool, &args).await,
        Command::Inspect { import_name } => inspect(&pool, &import_name).await,
    }
}

async fn migrate(pool: &PgPool) -> Result<()> {
    sqlx::query("CREATE SCHEMA IF NOT EXISTS benchmark_raw")
        .execute(pool)
        .await?;

    sqlx::raw_sql(
        "DO $$\
         BEGIN \
           IF to_regclass('benchmark_raw.checkpoint') IS NULL \
              AND to_regclass('benchmark_raw.paging_checkpoint') IS NOT NULL THEN \
             ALTER TABLE benchmark_raw.paging_checkpoint RENAME TO checkpoint; \
           END IF; \
         END \
         $$;",
    )
    .execute(pool)
    .await?;

    sqlx::query(
        "CREATE TABLE IF NOT EXISTS benchmark_raw.checkpoint (\
         import_name TEXT NOT NULL, \
         source_digest TEXT NOT NULL, \
         reader_mode TEXT NOT NULL, \
         definition_revision TEXT NOT NULL, \
         last_customer_id BIGINT NOT NULL, \
         committed_chunks BIGINT NOT NULL, \
         committed_rows BIGINT NOT NULL, \
         PRIMARY KEY (import_name, source_digest, reader_mode, definition_revision))",
    )
    .execute(pool)
    .await?;
    Ok(())
}

async fn lock_source(pool: &PgPool) -> Result<Transaction<'static, Postgres>> {
    let mut guard = pool.begin().await?;
    sqlx::query("LOCK TABLE app_source.source_customer IN SHARE MODE")
        .execute(&mut *guard)
        .await?;
    Ok(guard)
}

async fn source_digest<'e, E>(executor: E) -> Result<String>
where
    E: sqlx::Executor<'e, Database = Postgres>,
{
    let mut hasher = Sha256::new();
    let mut rows = sqlx::query_as::<_, (i64, String, bool, i64)>(
        "SELECT customer_id, full_name, is_active, balance_cents \
         FROM app_source.source_customer ORDER BY customer_id",
    )
    .fetch(executor);

    while let Some((customer_id, full_name, is_active, balance_cents)) = rows.try_next().await? {
        hasher.update(customer_id.to_le_bytes());
        hasher.update([0]);
        hasher.update(full_name.as_bytes());
        hasher.update([0]);
        hasher.update([u8::from(is_active)]);
        hasher.update(balance_cents.to_le_bytes());
        hasher.update([0xFF]);
    }
    Ok(hex(&hasher.finalize()))
}

fn validate_run_args(args: &RunArgs) -> Result<RunConfig> {
    if args.import_name.is_empty() {
        bail!("import_name must not be empty");
    }
    if !(1..=MAX_CHUNK_SIZE).contains(&args.chunk_size) {
        bail!("chunk_size must be between 1 and {MAX_CHUNK_SIZE}");
    }

    let read_batch_size = match args.reader {
        ReaderMode::Cursor => {
            if args.page_size.is_some() {
                bail!("--page-size is only valid with --reader paging");
            }
            args.fetch_size.unwrap_or(DEFAULT_FETCH_SIZE)
        }
        ReaderMode::Paging => {
            if args.fetch_size.is_some() {
                bail!("--fetch-size is only valid with --reader cursor");
            }
            args.page_size.unwrap_or(DEFAULT_PAGE_SIZE)
        }
    };
    if !(1..=MAX_READ_BATCH_SIZE).contains(&read_batch_size) {
        bail!("reader batch size must be between 1 and {MAX_READ_BATCH_SIZE}");
    }

    if args.fail_after_chunk == Some(0) {
        bail!("fail_after_chunk must be greater than zero");
    }

    let kill_pause = match (
        args.pause_at_chunk,
        args.pause_phase,
        args.pause_marker.as_ref(),
    ) {
        (None, None, None) => None,
        (Some(chunk), Some(phase), Some(marker)) if chunk > 0 && !marker.as_os_str().is_empty() => {
            Some(KillPause {
                chunk,
                phase,
                marker: marker.clone(),
            })
        }
        _ => bail!(
            "--pause-at-chunk, --pause-phase, and --pause-marker must be supplied together; \
             pause_at_chunk must be greater than zero"
        ),
    };
    if args.fail_after_chunk.is_some() && kill_pause.is_some() {
        bail!("typed failure injection and external-kill pause cannot be enabled together");
    }

    Ok(RunConfig {
        reader_mode: args.reader,
        chunk_size: args.chunk_size,
        read_batch_size,
        fail_after_chunk: args.fail_after_chunk,
        kill_pause,
    })
}

async fn run(pool: &PgPool, args: &RunArgs) -> Result<()> {
    let config = validate_run_args(args)?;

    let mut source_guard = lock_source(pool).await?;
    let digest = source_digest(&mut *source_guard).await?;
    let identity = ExecutionIdentity {
        import_name: &args.import_name,
        source_digest: &digest,
        reader_mode: config.reader_mode,
    };

    reject_conflicting_source_identity(pool, identity).await?;
    let checkpoint = load_checkpoint(pool, identity).await?;
    let mut state = RunState::new(config.chunk_size, checkpoint);

    match config.reader_mode {
        ReaderMode::Cursor => run_cursor(pool, identity, &config, &mut state).await?,
        ReaderMode::Paging => run_paging(pool, identity, &config, &mut state).await?,
    }
    flush_chunk(pool, identity, &config, &mut state).await?;

    source_guard.rollback().await?;
    let last_customer_id = state
        .durable_position
        .map(|value| value.to_string())
        .unwrap_or_else(|| "none".to_owned());
    println!(
        "pid={} source_digest={} reader_mode={} last_customer_id={} committed_chunks={} committed_rows={}",
        std::process::id(),
        digest,
        config.reader_mode.as_str(),
        last_customer_id,
        state.committed_chunks,
        state.committed_rows,
    );
    Ok(())
}

async fn reject_conflicting_source_identity(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
) -> Result<()> {
    let conflicting = sqlx::query_scalar::<_, String>(
        "SELECT source_digest \
         FROM benchmark_raw.checkpoint \
         WHERE import_name = $1 AND reader_mode = $2 AND definition_revision = $3 \
           AND source_digest <> $4 \
         ORDER BY source_digest LIMIT 1",
    )
    .bind(identity.import_name)
    .bind(identity.reader_mode.as_str())
    .bind(identity.definition_revision())
    .bind(identity.source_digest)
    .fetch_optional(pool)
    .await?;

    if let Some(existing_digest) = conflicting {
        bail!(
            "source digest changed for existing raw execution identity: import_name={} \
             reader_mode={} definition_revision={} existing_digest={} current_digest={}; \
             refusing to reuse or fork checkpoint state under the same import identity",
            identity.import_name,
            identity.reader_mode.as_str(),
            identity.definition_revision(),
            existing_digest,
            identity.source_digest,
        );
    }
    Ok(())
}

async fn load_checkpoint(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
) -> Result<(Option<i64>, u64, u64)> {
    let row = sqlx::query(
        "SELECT last_customer_id, committed_chunks, committed_rows \
         FROM benchmark_raw.checkpoint \
         WHERE import_name = $1 AND source_digest = $2 AND reader_mode = $3 AND definition_revision = $4",
    )
    .bind(identity.import_name)
    .bind(identity.source_digest)
    .bind(identity.reader_mode.as_str())
    .bind(identity.definition_revision())
    .fetch_optional(pool)
    .await?;

    match row {
        Some(row) => Ok((
            Some(row.try_get::<i64, _>("last_customer_id")?),
            u64::try_from(row.try_get::<i64, _>("committed_chunks")?)?,
            u64::try_from(row.try_get::<i64, _>("committed_rows")?)?,
        )),
        None => Ok((None, 0, 0)),
    }
}

async fn run_paging(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
    config: &RunConfig,
    state: &mut RunState,
) -> Result<()> {
    let mut read_position = state.durable_position;
    loop {
        let page = fetch_page(pool, read_position, config.read_batch_size).await?;
        if page.is_empty() {
            break;
        }
        read_position = Some(page.last().context("non-empty page")?.customer_id);
        for row in page {
            accept_source_row(pool, identity, config, state, row).await?;
        }
    }
    Ok(())
}

async fn fetch_page(
    pool: &PgPool,
    after_customer_id: Option<i64>,
    page_size: usize,
) -> Result<Vec<SourceRow>> {
    let page_size_i64 = i64::try_from(page_size)?;
    let mut rows = match after_customer_id {
        Some(customer_id) => sqlx::query(
            "SELECT customer_id, full_name, is_active, balance_cents \
             FROM app_source.source_customer \
             WHERE customer_id > $1 ORDER BY customer_id LIMIT $2",
        )
        .bind(customer_id)
        .bind(page_size_i64)
        .fetch(pool),
        None => sqlx::query(
            "SELECT customer_id, full_name, is_active, balance_cents \
             FROM app_source.source_customer ORDER BY customer_id LIMIT $1",
        )
        .bind(page_size_i64)
        .fetch(pool),
    };

    let mut page = Vec::with_capacity(page_size);
    while let Some(row) = rows.try_next().await? {
        page.push(source_row(&row)?);
    }
    Ok(page)
}

async fn run_cursor(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
    config: &RunConfig,
    state: &mut RunState,
) -> Result<()> {
    let mut cursor_tx = pool.begin().await?;
    declare_cursor(&mut cursor_tx, state.durable_position).await?;
    // FETCH count is SQL syntax, not a bindable value. read_batch_size is range-validated usize
    // and the cursor name is a constant, so this dynamic statement is injection-safe.
    let fetch_sql = format!(
        "FETCH FORWARD {} FROM {CURSOR_NAME}",
        config.read_batch_size
    );

    loop {
        let saw_row = {
            let mut rows = sqlx::query(AssertSqlSafe(fetch_sql.as_str())).fetch(&mut *cursor_tx);
            let mut saw_row = false;
            while let Some(row) = rows.try_next().await? {
                saw_row = true;
                accept_source_row(pool, identity, config, state, source_row(&row)?).await?;
            }
            saw_row
        };
        if !saw_row {
            break;
        }
    }

    cursor_tx.rollback().await?;
    Ok(())
}

async fn declare_cursor(
    tx: &mut Transaction<'_, Postgres>,
    after_customer_id: Option<i64>,
) -> Result<()> {
    // Resume position is a trusted i64 loaded from our own checkpoint; cursor name is constant.
    // PostgreSQL DECLARE syntax cannot bind these syntax positions, so audit explicitly below.
    let query = match after_customer_id {
        Some(customer_id) => format!(
            "DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR \
             SELECT customer_id, full_name, is_active, balance_cents \
             FROM app_source.source_customer \
             WHERE customer_id > {customer_id} ORDER BY customer_id"
        ),
        None => format!(
            "DECLARE {CURSOR_NAME} NO SCROLL CURSOR FOR \
             SELECT customer_id, full_name, is_active, balance_cents \
             FROM app_source.source_customer ORDER BY customer_id"
        ),
    };
    sqlx::query(AssertSqlSafe(query)).execute(&mut **tx).await?;
    Ok(())
}

fn source_row(row: &sqlx::postgres::PgRow) -> Result<SourceRow> {
    Ok(SourceRow {
        customer_id: row.try_get("customer_id")?,
        full_name: row.try_get("full_name")?,
        is_active: row.try_get("is_active")?,
        balance_cents: row.try_get("balance_cents")?,
    })
}

async fn accept_source_row(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
    config: &RunConfig,
    state: &mut RunState,
    row: SourceRow,
) -> Result<()> {
    state
        .chunk
        .push(project(identity.import_name, identity.source_digest, row)?);
    if state.chunk.len() == config.chunk_size {
        flush_chunk(pool, identity, config, state).await?;
    }
    Ok(())
}

async fn flush_chunk(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
    config: &RunConfig,
    state: &mut RunState,
) -> Result<()> {
    if state.chunk.is_empty() {
        return Ok(());
    }

    let chunk_last = state.chunk.last().context("non-empty chunk")?.customer_id;
    let checkpoint = CheckpointUpdate {
        last_customer_id: chunk_last,
        committed_chunks: state.committed_chunks + 1,
        committed_rows: state.committed_rows + state.chunk.len() as u64,
    };
    commit_chunk(pool, identity, config, &state.chunk, checkpoint).await?;
    state.committed_chunks = checkpoint.committed_chunks;
    state.committed_rows = checkpoint.committed_rows;
    state.durable_position = Some(chunk_last);
    state.chunk.clear();
    Ok(())
}

fn project(import_name: &str, digest: &str, source: SourceRow) -> Result<ProjectedRow> {
    if source.full_name.trim().is_empty() {
        bail!(
            "source full_name must not be empty for customer {}",
            source.customer_id
        );
    }
    let mut hasher = Sha256::new();
    hasher.update(source.customer_id.to_le_bytes());
    hasher.update([0]);
    hasher.update(source.full_name.as_bytes());
    hasher.update([0]);
    hasher.update([u8::from(source.is_active)]);
    hasher.update(source.balance_cents.to_le_bytes());
    let digest_bytes = hasher.finalize();
    let mut fingerprint = [0_u8; FINGERPRINT_LEN];
    fingerprint.copy_from_slice(&digest_bytes[..FINGERPRINT_LEN]);

    Ok(ProjectedRow {
        import_name: import_name.to_owned(),
        source_digest: digest.to_owned(),
        customer_id: source.customer_id,
        display_name: source.full_name.to_uppercase(),
        loyalty_score: source.balance_cents / 100,
        is_premium: source.balance_cents >= PREMIUM_THRESHOLD_CENTS,
        row_fingerprint: fingerprint,
    })
}

async fn commit_chunk(
    pool: &PgPool,
    identity: ExecutionIdentity<'_>,
    config: &RunConfig,
    chunk: &[ProjectedRow],
    checkpoint: CheckpointUpdate,
) -> Result<()> {
    let mut tx = pool.begin().await?;
    for batch in chunk.chunks(ROWS_PER_STATEMENT) {
        insert_batch(&mut tx, batch).await?;
    }

    if config.fail_after_chunk == Some(checkpoint.committed_chunks) {
        bail!(
            "injected failure after business writes before checkpoint/commit at chunk {}",
            checkpoint.committed_chunks
        );
    }

    sqlx::query(
        "INSERT INTO benchmark_raw.checkpoint \
         (import_name, source_digest, reader_mode, definition_revision, last_customer_id, committed_chunks, committed_rows) \
         VALUES ($1, $2, $3, $4, $5, $6, $7) \
         ON CONFLICT (import_name, source_digest, reader_mode, definition_revision) \
         DO UPDATE SET last_customer_id = EXCLUDED.last_customer_id, \
                       committed_chunks = EXCLUDED.committed_chunks, \
                       committed_rows = EXCLUDED.committed_rows",
    )
    .bind(identity.import_name)
    .bind(identity.source_digest)
    .bind(identity.reader_mode.as_str())
    .bind(identity.definition_revision())
    .bind(checkpoint.last_customer_id)
    .bind(i64::try_from(checkpoint.committed_chunks)?)
    .bind(i64::try_from(checkpoint.committed_rows)?)
    .execute(&mut *tx)
    .await?;

    pause_for_external_kill(
        config.kill_pause.as_ref(),
        checkpoint.committed_chunks,
        PausePhase::BeforeCommit,
    )
    .await?;

    tx.commit().await?;

    pause_for_external_kill(
        config.kill_pause.as_ref(),
        checkpoint.committed_chunks,
        PausePhase::AfterCommit,
    )
    .await?;
    Ok(())
}

async fn pause_for_external_kill(
    pause: Option<&KillPause>,
    chunk: u64,
    phase: PausePhase,
) -> Result<()> {
    let Some(pause) = pause else {
        return Ok(());
    };
    if pause.chunk != chunk || pause.phase != phase {
        return Ok(());
    }

    write_pause_marker(&pause.marker, chunk, phase)?;
    std::future::pending::<Result<()>>().await
}

fn write_pause_marker(marker: &Path, chunk: u64, phase: PausePhase) -> Result<()> {
    let mut file = File::create(marker)
        .with_context(|| format!("create external-kill marker {}", marker.display()))?;
    writeln!(
        file,
        "pid={} chunk={} phase={}",
        std::process::id(),
        chunk,
        phase.as_str()
    )?;
    file.sync_all()?;
    Ok(())
}

async fn insert_batch(tx: &mut Transaction<'_, Postgres>, batch: &[ProjectedRow]) -> Result<()> {
    let bound_parameters = batch
        .len()
        .checked_mul(COLUMNS_PER_ROW)
        .context("writer bind count overflow")?;
    if batch.len() > ROWS_PER_STATEMENT || bound_parameters > MAX_BOUND_PARAMETERS {
        bail!(
            "writer batch exceeds parity bound of {ROWS_PER_STATEMENT} rows / {MAX_BOUND_PARAMETERS} parameters"
        );
    }
    let mut builder = QueryBuilder::<Postgres>::new(
        "INSERT INTO app_business.customer_projection \
         (import_name, source_digest, customer_id, display_name, loyalty_score, is_premium, row_fingerprint) ",
    );
    builder.push_values(batch, |mut separated, row| {
        separated
            .push_bind(&row.import_name)
            .push_bind(&row.source_digest)
            .push_bind(row.customer_id)
            .push_bind(&row.display_name)
            .push_bind(row.loyalty_score)
            .push_bind(row.is_premium)
            .push_bind(row.row_fingerprint.as_slice());
    });
    builder.build().execute(&mut **tx).await?;
    Ok(())
}

async fn inspect(pool: &PgPool, import_name: &str) -> Result<()> {
    let mut rows = sqlx::query(
        "SELECT source_digest, reader_mode, definition_revision, last_customer_id, committed_chunks, committed_rows \
         FROM benchmark_raw.checkpoint \
         WHERE import_name = $1 \
         ORDER BY reader_mode, definition_revision, source_digest",
    )
    .bind(import_name)
    .fetch(pool);

    while let Some(row) = rows.try_next().await? {
        println!(
            "source_digest={} reader_mode={} definition_revision={} last_customer_id={} committed_chunks={} committed_rows={}",
            row.try_get::<String, _>("source_digest")?,
            row.try_get::<String, _>("reader_mode")?,
            row.try_get::<String, _>("definition_revision")?,
            row.try_get::<i64, _>("last_customer_id")?,
            row.try_get::<i64, _>("committed_chunks")?,
            row.try_get::<i64, _>("committed_rows")?,
        );
    }
    Ok(())
}

fn hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        out.push(char::from(HEX[(byte >> 4) as usize]));
        out.push(char::from(HEX[(byte & 0x0f) as usize]));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run_args(reader: ReaderMode) -> RunArgs {
        RunArgs {
            import_name: "import".to_owned(),
            reader,
            chunk_size: 1_000,
            fetch_size: None,
            page_size: None,
            fail_after_chunk: None,
            pause_at_chunk: None,
            pause_phase: None,
            pause_marker: None,
        }
    }

    #[test]
    fn cli_accepts_required_database_url_before_subcommand() {
        let parsed = Cli::try_parse_from([
            "raw-sqlx",
            "--database-url",
            "postgres://postgres:postgres@localhost:5432/postgres",
            "migrate",
        ]);
        assert!(parsed.is_ok());
    }

    #[test]
    fn run_requires_explicit_reader_mode() {
        let parsed = Cli::try_parse_from([
            "raw-sqlx",
            "--database-url",
            "postgres://postgres:postgres@localhost:5432/postgres",
            "run",
            "--import-name",
            "import",
        ]);
        assert!(parsed.is_err());
    }

    #[test]
    fn writer_bound_matches_oxide_batch_060_contract() {
        assert_eq!(ROWS_PER_STATEMENT, 285);
        assert_eq!(MAX_BOUND_PARAMETERS, 1_995);
        assert_eq!(1_000_usize.div_ceil(ROWS_PER_STATEMENT), 4);
    }

    #[test]
    fn reader_arguments_are_mode_specific_and_bounded() {
        let mut cursor = run_args(ReaderMode::Cursor);
        cursor.fetch_size = Some(1);
        assert!(validate_run_args(&cursor).is_ok_and(|value| value.read_batch_size == 1));
        cursor.fetch_size = Some(MAX_READ_BATCH_SIZE + 1);
        assert!(validate_run_args(&cursor).is_err());
        cursor.fetch_size = None;
        cursor.page_size = Some(10);
        assert!(validate_run_args(&cursor).is_err());

        let mut paging = run_args(ReaderMode::Paging);
        paging.page_size = Some(1);
        assert!(validate_run_args(&paging).is_ok_and(|value| value.read_batch_size == 1));
        paging.page_size = None;
        paging.fetch_size = Some(10);
        assert!(validate_run_args(&paging).is_err());
    }

    #[test]
    fn run_arguments_reject_invalid_failure_controls() {
        let mut args = run_args(ReaderMode::Paging);
        args.fail_after_chunk = Some(0);
        assert!(validate_run_args(&args).is_err());

        args.fail_after_chunk = None;
        args.pause_at_chunk = Some(3);
        assert!(validate_run_args(&args).is_err());

        args.pause_phase = Some(PausePhase::BeforeCommit);
        args.pause_marker = Some(PathBuf::from("marker"));
        assert!(validate_run_args(&args).is_ok());

        args.fail_after_chunk = Some(2);
        assert!(validate_run_args(&args).is_err());
    }

    #[test]
    fn reader_modes_have_distinct_restart_identity_revisions() {
        assert_ne!(
            ReaderMode::Cursor.definition_revision(),
            ReaderMode::Paging.definition_revision()
        );
    }

    #[test]
    fn projection_matches_documented_transform() {
        let result = project(
            "import",
            "digest",
            SourceRow {
                customer_id: 7,
                full_name: "Alice Smith".to_owned(),
                is_active: true,
                balance_cents: 50_099,
            },
        );
        let Ok(projected) = result else {
            panic!("valid source row must project successfully");
        };
        assert_eq!(projected.display_name, "ALICE SMITH");
        assert_eq!(projected.loyalty_score, 500);
        assert!(projected.is_premium);
    }
}
