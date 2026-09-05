use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use futures_util::TryStreamExt;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Postgres, QueryBuilder, Row, Transaction};

const READER_MODE: &str = "paging";
const DEFINITION_REVISION: &str = "raw-sqlx-postgres-postgres-paging-v1";
const COLUMNS_PER_ROW: usize = 7;
const MAX_PARAMETERS_PER_STATEMENT: usize = 2_000;
const ROWS_PER_STATEMENT: usize = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW;
const MAX_BOUND_PARAMETERS: usize = ROWS_PER_STATEMENT * COLUMNS_PER_ROW;
const FINGERPRINT_LEN: usize = 16;
const PREMIUM_THRESHOLD_CENTS: i64 = 50_000;

#[derive(Parser)]
#[command(name = "raw-sqlx")]
struct Cli {
    #[arg(long, env = "DATABASE_URL", global = true)]
    database_url: String,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Migrate,
    Run {
        #[arg(long)]
        import_name: String,
        #[arg(long, default_value_t = 1_000)]
        chunk_size: usize,
        #[arg(long, default_value_t = 750)]
        page_size: i64,
        #[arg(long)]
        fail_after_chunk: Option<u64>,
    },
    Inspect {
        #[arg(long)]
        import_name: String,
    },
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

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let pool = PgPool::connect(&cli.database_url)
        .await
        .context("connect to PostgreSQL")?;

    match cli.command {
        Command::Migrate => migrate(&pool).await,
        Command::Run {
            import_name,
            chunk_size,
            page_size,
            fail_after_chunk,
        } => run(&pool, &import_name, chunk_size, page_size, fail_after_chunk).await,
        Command::Inspect { import_name } => inspect(&pool, &import_name).await,
    }
}

async fn migrate(pool: &PgPool) -> Result<()> {
    sqlx::query("CREATE SCHEMA IF NOT EXISTS benchmark_raw")
        .execute(pool)
        .await?;
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS benchmark_raw.paging_checkpoint (\
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

async fn run(
    pool: &PgPool,
    import_name: &str,
    chunk_size: usize,
    page_size: i64,
    fail_after_chunk: Option<u64>,
) -> Result<()> {
    if import_name.is_empty() {
        bail!("import_name must not be empty");
    }
    if chunk_size == 0 {
        bail!("chunk_size must be > 0");
    }
    if page_size <= 0 {
        bail!("page_size must be > 0");
    }

    migrate(pool).await?;
    let mut source_guard = lock_source(pool).await?;
    let digest = source_digest(&mut *source_guard).await?;
    let (mut durable_position, mut committed_chunks, mut committed_rows) =
        load_checkpoint(pool, import_name, &digest).await?;
    let mut read_position = durable_position;

    let mut chunk = Vec::with_capacity(chunk_size);
    loop {
        let page = fetch_page(pool, read_position, page_size).await?;
        if page.is_empty() {
            break;
        }
        read_position = page.last().context("non-empty page")?.customer_id;
        for row in page {
            chunk.push(project(import_name, &digest, row)?);
            if chunk.len() == chunk_size {
                let chunk_last = chunk.last().context("non-empty chunk")?.customer_id;
                commit_chunk(
                    pool,
                    import_name,
                    &digest,
                    &chunk,
                    chunk_last,
                    committed_chunks + 1,
                    committed_rows + chunk.len() as u64,
                    fail_after_chunk,
                )
                .await?;
                committed_chunks += 1;
                committed_rows += chunk.len() as u64;
                durable_position = chunk_last;
                chunk.clear();
            }
        }
    }

    if !chunk.is_empty() {
        let chunk_last = chunk.last().context("non-empty final chunk")?.customer_id;
        commit_chunk(
            pool,
            import_name,
            &digest,
            &chunk,
            chunk_last,
            committed_chunks + 1,
            committed_rows + chunk.len() as u64,
            fail_after_chunk,
        )
        .await?;
        committed_chunks += 1;
        committed_rows += chunk.len() as u64;
        durable_position = chunk_last;
    }

    println!(
        "source_digest={digest} reader_mode={READER_MODE} last_customer_id={durable_position} committed_chunks={committed_chunks} committed_rows={committed_rows}"
    );
    source_guard.rollback().await?;
    Ok(())
}

async fn load_checkpoint(pool: &PgPool, import_name: &str, digest: &str) -> Result<(i64, u64, u64)> {
    let row = sqlx::query(
        "SELECT last_customer_id, committed_chunks, committed_rows \
         FROM benchmark_raw.paging_checkpoint \
         WHERE import_name = $1 AND source_digest = $2 AND reader_mode = $3 AND definition_revision = $4",
    )
    .bind(import_name)
    .bind(digest)
    .bind(READER_MODE)
    .bind(DEFINITION_REVISION)
    .fetch_optional(pool)
    .await?;

    match row {
        Some(row) => Ok((
            row.try_get::<i64, _>("last_customer_id")?,
            u64::try_from(row.try_get::<i64, _>("committed_chunks")?)?,
            u64::try_from(row.try_get::<i64, _>("committed_rows")?)?,
        )),
        None => Ok((i64::MIN, 0, 0)),
    }
}

async fn fetch_page(pool: &PgPool, last_customer_id: i64, page_size: i64) -> Result<Vec<SourceRow>> {
    let mut rows = sqlx::query(
        "SELECT customer_id, full_name, is_active, balance_cents \
         FROM app_source.source_customer \
         WHERE customer_id > $1 ORDER BY customer_id LIMIT $2",
    )
    .bind(last_customer_id)
    .bind(page_size)
    .fetch(pool);

    let mut page = Vec::with_capacity(usize::try_from(page_size)?);
    while let Some(row) = rows.try_next().await? {
        page.push(SourceRow {
            customer_id: row.try_get("customer_id")?,
            full_name: row.try_get("full_name")?,
            is_active: row.try_get("is_active")?,
            balance_cents: row.try_get("balance_cents")?,
        });
    }
    Ok(page)
}

fn project(import_name: &str, digest: &str, source: SourceRow) -> Result<ProjectedRow> {
    if source.full_name.trim().is_empty() {
        bail!("source full_name must not be empty for customer {}", source.customer_id);
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
    import_name: &str,
    digest: &str,
    chunk: &[ProjectedRow],
    last_customer_id: i64,
    committed_chunks: u64,
    committed_rows: u64,
    fail_after_chunk: Option<u64>,
) -> Result<()> {
    let mut tx = pool.begin().await?;
    for batch in chunk.chunks(ROWS_PER_STATEMENT) {
        insert_batch(&mut tx, batch).await?;
    }

    if fail_after_chunk == Some(committed_chunks) {
        bail!("injected failure after business writes before checkpoint/commit at chunk {committed_chunks}");
    }

    sqlx::query(
        "INSERT INTO benchmark_raw.paging_checkpoint \
         (import_name, source_digest, reader_mode, definition_revision, last_customer_id, committed_chunks, committed_rows) \
         VALUES ($1, $2, $3, $4, $5, $6, $7) \
         ON CONFLICT (import_name, source_digest, reader_mode, definition_revision) \
         DO UPDATE SET last_customer_id = EXCLUDED.last_customer_id, \
                       committed_chunks = EXCLUDED.committed_chunks, \
                       committed_rows = EXCLUDED.committed_rows",
    )
    .bind(import_name)
    .bind(digest)
    .bind(READER_MODE)
    .bind(DEFINITION_REVISION)
    .bind(last_customer_id)
    .bind(i64::try_from(committed_chunks)?)
    .bind(i64::try_from(committed_rows)?)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

async fn insert_batch(tx: &mut Transaction<'_, Postgres>, batch: &[ProjectedRow]) -> Result<()> {
    if batch.len() > ROWS_PER_STATEMENT {
        bail!("writer batch exceeds parity bound of {ROWS_PER_STATEMENT} rows");
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
        "SELECT source_digest, last_customer_id, committed_chunks, committed_rows \
         FROM benchmark_raw.paging_checkpoint \
         WHERE import_name = $1 AND reader_mode = $2 AND definition_revision = $3 \
         ORDER BY source_digest",
    )
    .bind(import_name)
    .bind(READER_MODE)
    .bind(DEFINITION_REVISION)
    .fetch(pool);

    while let Some(row) = rows.try_next().await? {
        println!(
            "source_digest={} last_customer_id={} committed_chunks={} committed_rows={}",
            row.try_get::<String, _>("source_digest")?,
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

    #[test]
    fn writer_bound_matches_oxide_batch_060_contract() {
        assert_eq!(ROWS_PER_STATEMENT, 285);
        assert_eq!(MAX_BOUND_PARAMETERS, 1_995);
        assert_eq!(1_000_usize.div_ceil(ROWS_PER_STATEMENT), 4);
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
