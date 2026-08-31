//! Shared test-only helpers. Every scenario drives the real, compiled
//! `csv-postgres` binary as a child process (never an in-process function
//! call) against a real PostgreSQL database, and asserts on database state
//! -- never on log strings (spec ss18).
//!
//! Test isolation (spec ss42): every test gets a unique `import_name` (job
//! identity) and a unique `customer_id` range (`--id-offset`), both derived
//! from a nanosecond nonce, so tests sharing one PostgreSQL database never
//! collide even under parallel `cargo test` execution.
//!
//! Each `tests/*.rs` file compiles this module separately as its own copy,
//! and no single test file uses every helper here.
#![allow(dead_code)]

use std::path::PathBuf;
use std::process::{Command, Output};
use std::time::{SystemTime, UNIX_EPOCH};

use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;

pub fn database_url() -> String {
    std::env::var("CSV_POSTGRES_TEST_DATABASE_URL").unwrap_or_else(|_| {
        "postgresql://algorist@localhost:5432/oxide_batch_workload_csv_postgres".to_owned()
    })
}

pub async fn pool() -> PgPool {
    PgPoolOptions::new()
        .connect(&database_url())
        .await
        .expect("connect to test database")
}

/// A per-process-invocation nonce: nanosecond timestamp is not, by itself,
/// guaranteed unique across two calls in the same nanosecond, so callers
/// that need multiple distinct ids in one test combine this with a suffix.
pub fn nonce() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time moves forward")
        .as_nanos()
}

pub fn unique_name(label: &str) -> String {
    format!("csvpg_{label}_{}", nonce())
}

/// A `customer_id` base offset unique to this test invocation, spaced far
/// enough apart (1,000,000) that even a stress-sized dataset from one test
/// cannot reach into another's range.
pub fn unique_id_offset() -> u64 {
    (nonce() % 1_000_000_000) as u64 * 1_000_000
}

pub fn temp_csv(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!("csv-postgres-test-{label}-{}.csv", nonce()))
}

pub fn bin() -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_csv-postgres"));
    cmd.env("DATABASE_URL", database_url());
    cmd
}

/// Runs `cmd`, panicking with stdout/stderr on a nonzero exit -- for setup
/// steps (`migrate`, `generate`) that must succeed for the test to mean
/// anything.
pub fn run_ok(cmd: &mut Command) -> Output {
    let output = cmd.output().expect("spawn csv-postgres");
    assert!(
        output.status.success(),
        "command did not succeed: status={:?}\nstdout={}\nstderr={}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
    output
}

/// Ensures both schemas exist. Idempotent (`CREATE ... IF NOT EXISTS`), so
/// every test calls this rather than relying on external setup order.
pub fn migrate() {
    run_ok(bin().arg("migrate"));
}

pub fn recover(import_name: &str, input: &std::path::Path) -> Output {
    run_ok(
        bin()
            .arg("recover")
            .arg("--import-name")
            .arg(import_name)
            .arg("--input")
            .arg(input),
    )
}

pub struct Dataset {
    pub path: PathBuf,
    pub id_offset: u64,
    pub rows: u64,
}

pub struct GenerateOptions<'a> {
    pub rows: u64,
    pub seed: u64,
    pub duplicate_at: Option<u64>,
    pub malformed_at: Option<u64>,
    pub bad_amount_at: Option<u64>,
    pub label: &'a str,
}

impl Default for GenerateOptions<'_> {
    fn default() -> Self {
        Self {
            rows: 1_000,
            seed: 42,
            duplicate_at: None,
            malformed_at: None,
            bad_amount_at: None,
            label: "dataset",
        }
    }
}

pub fn generate(options: GenerateOptions<'_>) -> Dataset {
    let path = temp_csv(options.label);
    let id_offset = unique_id_offset();
    let mut cmd = bin();
    cmd.arg("generate")
        .arg("--output")
        .arg(&path)
        .arg("--rows")
        .arg(options.rows.to_string())
        .arg("--seed")
        .arg(options.seed.to_string())
        .arg("--id-offset")
        .arg(id_offset.to_string());
    if let Some(at) = options.duplicate_at {
        cmd.arg("--inject-duplicate-at").arg(at.to_string());
    }
    if let Some(at) = options.malformed_at {
        cmd.arg("--inject-malformed-at").arg(at.to_string());
    }
    if let Some(at) = options.bad_amount_at {
        cmd.arg("--inject-bad-amount-at").arg(at.to_string());
    }
    run_ok(&mut cmd);
    Dataset {
        path,
        id_offset,
        rows: options.rows,
    }
}

pub async fn business_row_count_in_range(pool: &PgPool, id_offset: u64, rows: u64) -> i64 {
    let low = id_offset as i64;
    let high = (id_offset + rows) as i64;
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM app_business.imported_customer WHERE customer_id > $1 AND customer_id <= $2",
    )
    .bind(low)
    .bind(high)
    .fetch_one(pool)
    .await
    .expect("count business rows")
}

pub async fn job_instance_count(pool: &PgPool, job_name: &str) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM oxide_batch.ob_job_instance WHERE job_name = $1")
        .bind(job_name)
        .fetch_one(pool)
        .await
        .expect("count job instances")
}

pub async fn latest_execution_status(pool: &PgPool, import_name: &str) -> Option<(String, String)> {
    sqlx::query_as::<_, (String, String)>(
        "SELECT e.status, e.exit_code \
         FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt DESC LIMIT 1",
    )
    .bind(import_name)
    .fetch_optional(pool)
    .await
    .expect("query latest execution status")
}

/// Like `canonical_digest_in_range`, but excludes `customer_id` *and*
/// `email` from the hash: two datasets generated from the same seed at
/// *different* `id_offset`s (as any two independent test runs sharing one
/// business table must be) have identical `name`/`amount`/`created_at` for
/// row N, but `email` is deliberately derived from `customer_id`
/// (`generator::base_row`) and so is not offset-invariant by design. This
/// digest lets a clean run and a crash/restart run -- which necessarily
/// used different offsets -- be compared on genuinely offset-independent
/// content (spec ss27), not just row counts.
pub async fn content_digest_in_range(pool: &PgPool, id_offset: u64, rows: u64) -> String {
    let low = id_offset as i64;
    let high = (id_offset + rows) as i64;
    let rows: Vec<(String, i64, chrono::DateTime<chrono::Utc>)> = sqlx::query_as(
        "SELECT name, amount, created_at \
         FROM app_business.imported_customer \
         WHERE customer_id > $1 AND customer_id <= $2 \
         ORDER BY customer_id",
    )
    .bind(low)
    .bind(high)
    .fetch_all(pool)
    .await
    .expect("select business rows for content digest");

    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    for (name, amount, created_at) in &rows {
        hasher.update(name.as_bytes());
        hasher.update(amount.to_le_bytes());
        hasher.update(created_at.to_rfc3339().as_bytes());
        hasher.update([0xFFu8]);
    }
    format!("{:x}", hasher.finalize())
}

pub async fn canonical_digest_in_range(pool: &PgPool, id_offset: u64, rows: u64) -> String {
    let low = id_offset as i64;
    let high = (id_offset + rows) as i64;
    let rows: Vec<(i64, String, String, i64, chrono::DateTime<chrono::Utc>)> = sqlx::query_as(
        "SELECT customer_id, name, email, amount, created_at \
         FROM app_business.imported_customer \
         WHERE customer_id > $1 AND customer_id <= $2 \
         ORDER BY customer_id",
    )
    .bind(low)
    .bind(high)
    .fetch_all(pool)
    .await
    .expect("select business rows for digest");

    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    for (customer_id, name, email, amount, created_at) in &rows {
        hasher.update(customer_id.to_le_bytes());
        hasher.update(name.as_bytes());
        hasher.update(email.as_bytes());
        hasher.update(amount.to_le_bytes());
        hasher.update(created_at.to_rfc3339().as_bytes());
        hasher.update([0xFFu8]);
    }
    format!("{:x}", hasher.finalize())
}
