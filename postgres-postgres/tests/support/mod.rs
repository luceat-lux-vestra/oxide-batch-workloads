//! Shared test-only helpers. Every scenario drives the real, compiled
//! `postgres-postgres` binary as a child process (never an in-process
//! function call) against a real PostgreSQL database, and asserts on
//! database state -- never on log strings.
//!
//! # What is, and is not, safe to run concurrently
//!
//! Every test gets a unique `import_name` (job identity) and a unique
//! `customer_id` range (`--id-offset`), both derived from a nanosecond
//! nonce, so tests that only ever touch their own scoped rows (their own
//! `import_name`, their own `id_offset` range) cannot collide with each
//! other's *identity*, regardless of execution order.
//!
//! That is not the same claim as "safe under parallel execution." `run`
//! and `verify` both cover the *entire* `app_source.source_customer` table
//! by design (see [`reset`]'s own doc comment below), and `seed` never
//! truncates -- so any test that asserts an exact row count, an exact
//! digest-scoped destination count, or that calls [`reset`] is making a
//! claim about *global* database state, not just its own nonce-scoped
//! slice of it. Those tests require serialized execution: this workload's
//! `ci/validate` runs `cargo test -- --test-threads=1`, and relies on
//! `cargo test`'s own default of running separate test binaries (files)
//! one at a time, not concurrently -- it does not itself add any
//! additional cross-binary locking. Do not run this suite with a test
//! runner that parallelizes across binaries (e.g. `cargo nextest`'s
//! default) without first auditing which tests depend on that ordering.
//!
//! Each `tests/*.rs` file compiles this module separately as its own copy,
//! and no single test file uses every helper here.
#![allow(dead_code)]

use std::process::{Command, Output};
use std::time::{SystemTime, UNIX_EPOCH};

use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;

/// Matches `../../docker-compose.yml` and `../../ci/validate`'s own
/// `DATABASE_URL` exactly -- not a personal local development URL, so a
/// contributor without `POSTGRES_POSTGRES_TEST_DATABASE_URL` set still
/// gets a default that actually matches the checked-in service this
/// workload starts.
const DEFAULT_TEST_DATABASE_URL: &str =
    "postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5434/postgres_postgres_workload";

pub fn database_url() -> String {
    std::env::var("POSTGRES_POSTGRES_TEST_DATABASE_URL")
        .unwrap_or_else(|_| DEFAULT_TEST_DATABASE_URL.to_owned())
}

pub async fn pool() -> PgPool {
    PgPoolOptions::new()
        .connect(&database_url())
        .await
        .expect("connect to test database")
}

/// A per-process-invocation nonce: not, by itself, guaranteed unique across
/// two calls in the same nanosecond, so callers that need multiple distinct
/// ids in one test combine this with a suffix.
pub fn nonce() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time moves forward")
        .as_nanos()
}

pub fn unique_name(label: &str) -> String {
    format!("pgpg_{label}_{}", nonce())
}

/// A `customer_id` base offset unique to this test invocation, spaced far
/// enough apart (1,000,000) that even a large dataset from one test cannot
/// reach into another's range.
pub fn unique_id_offset() -> u64 {
    (nonce() % 1_000_000_000) as u64 * 1_000_000
}

pub fn bin() -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_postgres-postgres"));
    cmd.env("DATABASE_URL", database_url());
    cmd
}

/// Runs `cmd`, panicking with stdout/stderr on a nonzero exit -- for setup
/// steps (`migrate`, `seed`) that must succeed for the test to mean
/// anything.
pub fn run_ok(cmd: &mut Command) -> Output {
    let output = cmd.output().expect("spawn postgres-postgres");
    assert!(
        output.status.success(),
        "command did not succeed: status={:?}\nstdout={}\nstderr={}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
    output
}

/// Ensures both `oxide_batch` and workload-owned schemas exist. Idempotent
/// (`CREATE ... IF NOT EXISTS`), so every test calls this rather than
/// relying on external setup order.
pub fn migrate() {
    run_ok(bin().arg("migrate"));
}

/// Truncates `app_source`/`app_business` (never `oxide_batch`). `run`'s
/// source identity digest and `verify`'s comparison both cover the
/// *entire* `app_source.source_customer` table by design (mirroring a real
/// deployment, whose source table only ever holds its own data -- see the
/// crate README's "Source identity" section), not just one test's own
/// `--id-offset` range. Callers that assert an exact row count or an exact
/// digest-scoped destination count must call this first; callers that only
/// assert within their own `id_offset` range, or only assert a specific
/// `customer_id`'s presence/absence, do not need to.
pub fn reset() {
    run_ok(bin().arg("reset"));
}

pub struct Dataset {
    pub id_offset: u64,
    pub rows: u64,
    pub seed: u64,
}

pub struct SeedOptions {
    pub rows: u64,
    pub seed: u64,
}

impl Default for SeedOptions {
    fn default() -> Self {
        Self {
            rows: 1_000,
            seed: 42,
        }
    }
}

pub fn seed(options: SeedOptions) -> Dataset {
    seed_at(options, unique_id_offset())
}

/// Like [`seed`], but with an explicit `id_offset` instead of a fresh
/// nonce-derived one -- for tests that need two seed calls to produce
/// byte-identical `customer_id` ranges (e.g. proving the generator itself
/// is deterministic for a fixed `(rows, seed, id_offset)`), which `seed`'s
/// own per-call nonce would otherwise defeat.
pub fn seed_at(options: SeedOptions, id_offset: u64) -> Dataset {
    run_ok(
        bin()
            .arg("seed")
            .arg("--rows")
            .arg(options.rows.to_string())
            .arg("--seed")
            .arg(options.seed.to_string())
            .arg("--id-offset")
            .arg(id_offset.to_string()),
    );
    Dataset {
        id_offset,
        rows: options.rows,
        seed: options.seed,
    }
}

pub fn run(import_name: &str, chunk_size: u32) -> Output {
    run_ok(
        bin()
            .arg("run")
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(chunk_size.to_string()),
    )
}

pub fn run_with_fetch_size(import_name: &str, chunk_size: u32, fetch_size: usize) -> Output {
    run_ok(
        bin()
            .arg("run")
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(chunk_size.to_string())
            .arg("--fetch-size")
            .arg(fetch_size.to_string()),
    )
}

/// Like [`run_with_fetch_size`], but starts the child process and returns
/// immediately without waiting for it -- for tests that need to observe or
/// interact with database state *while* a run is still in flight (e.g.
/// `tests/source_stability.rs`, which attacks the window between
/// `job::run`'s source-stability guard being taken and released).
pub fn spawn_run_with_fetch_size(
    import_name: &str,
    chunk_size: u32,
    fetch_size: usize,
) -> std::process::Child {
    bin()
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(chunk_size.to_string())
        .arg("--fetch-size")
        .arg(fetch_size.to_string())
        .spawn()
        .expect("spawn postgres-postgres run in the background")
}

pub fn verify(import_name: &str) -> std::process::Output {
    bin()
        .arg("verify")
        .arg("--import-name")
        .arg(import_name)
        .output()
        .expect("spawn postgres-postgres verify")
}

pub async fn source_row_count_in_range(pool: &PgPool, id_offset: u64, rows: u64) -> i64 {
    let low = id_offset as i64;
    let high = (id_offset + rows) as i64;
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM app_source.source_customer WHERE customer_id > $1 AND customer_id <= $2",
    )
    .bind(low)
    .bind(high)
    .fetch_one(pool)
    .await
    .expect("count source rows")
}

pub async fn destination_row_count(pool: &PgPool, import_name: &str, source_digest: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM app_business.customer_projection \
         WHERE import_name = $1 AND source_digest = $2",
    )
    .bind(import_name)
    .bind(source_digest)
    .fetch_one(pool)
    .await
    .expect("count destination rows")
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

pub async fn job_instance_count(pool: &PgPool, job_name: &str) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM oxide_batch.ob_job_instance WHERE job_name = $1")
        .bind(job_name)
        .fetch_one(pool)
        .await
        .expect("count job instances")
}
