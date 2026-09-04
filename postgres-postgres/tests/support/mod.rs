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

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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

/// Cursor mode, driven explicitly (`--reader cursor`) -- see the module
/// documentation for why every helper here names its reader mode rather
/// than relying on an implicit default (there is none; `--reader` is a
/// required CLI flag).
pub fn run_cursor(import_name: &str, chunk_size: u32) -> Output {
    run_ok(
        bin()
            .arg("run")
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(chunk_size.to_string())
            .arg("--reader")
            .arg("cursor"),
    )
}

pub fn run_cursor_with_fetch_size(import_name: &str, chunk_size: u32, fetch_size: usize) -> Output {
    run_ok(
        bin()
            .arg("run")
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(chunk_size.to_string())
            .arg("--reader")
            .arg("cursor")
            .arg("--fetch-size")
            .arg(fetch_size.to_string()),
    )
}

/// Like [`run_cursor_with_fetch_size`], but starts the child process and
/// returns immediately without waiting for it -- for tests that need to
/// observe or interact with database state *while* a run is still in
/// flight (e.g. `tests/source_stability.rs`, which attacks the window
/// between `job::run`'s source-stability guard being taken and released).
pub fn spawn_run_cursor_with_fetch_size(
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
        .arg("--reader")
        .arg("cursor")
        .arg("--fetch-size")
        .arg(fetch_size.to_string())
        .spawn()
        .expect("spawn postgres-postgres run in the background")
}

/// Paging mode, driven explicitly (`--reader paging`).
pub fn run_paging(import_name: &str, chunk_size: u32) -> Output {
    run_ok(
        bin()
            .arg("run")
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(chunk_size.to_string())
            .arg("--reader")
            .arg("paging"),
    )
}

pub fn run_paging_with_page_size(import_name: &str, chunk_size: u32, page_size: usize) -> Output {
    run_ok(
        bin()
            .arg("run")
            .arg("--import-name")
            .arg(import_name)
            .arg("--chunk-size")
            .arg(chunk_size.to_string())
            .arg("--reader")
            .arg("paging")
            .arg("--page-size")
            .arg(page_size.to_string()),
    )
}

/// Like [`run_paging_with_page_size`], but starts the child process and
/// returns immediately without waiting for it -- the paging counterpart of
/// [`spawn_run_cursor_with_fetch_size`], for the paging source-stability
/// adversarial test.
pub fn spawn_run_paging_with_page_size(
    import_name: &str,
    chunk_size: u32,
    page_size: usize,
) -> std::process::Child {
    bin()
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(chunk_size.to_string())
        .arg("--reader")
        .arg("paging")
        .arg("--page-size")
        .arg(page_size.to_string())
        .spawn()
        .expect("spawn postgres-postgres run in the background")
}

// -------------------------------------------------------------------------
// Campaign #63 PR 3: deterministic rollback / crash / recovery evidence.
// -------------------------------------------------------------------------

/// A marker file path unique to this test invocation, in the same temp
/// directory `std::env::temp_dir()` other workloads in this repository use
/// for test-scoped scratch files (see `csv-postgres/tests/support/mod.rs`'s
/// `temp_csv`).
pub fn temp_marker(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!("postgres-postgres-test-{label}-{}.marker", nonce()))
}

/// Starts the real compiled binary in the background with fault injection
/// armed, returning the still-running `Child` immediately -- for the
/// hard-crash tests, which must observe the failpoint's marker file and
/// kill this exact child while it is deliberately paused mid-chunk (see
/// `src/failpoint.rs`'s module documentation). `size_flag` is
/// `("--fetch-size", fetch_size)` for cursor or `("--page-size",
/// page_size)` for paging -- both mode-specific reader configuration.
#[allow(clippy::too_many_arguments)]
pub fn spawn_run_with_failpoint(
    reader_mode: &str,
    import_name: &str,
    chunk_size: u32,
    size_flag: (&str, usize),
    fail_at_chunk: u32,
    failure_mode: &str,
    pause_marker: &Path,
) -> Child {
    bin()
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(chunk_size.to_string())
        .arg("--reader")
        .arg(reader_mode)
        .arg(size_flag.0)
        .arg(size_flag.1.to_string())
        .arg("--fail-at-chunk")
        .arg(fail_at_chunk.to_string())
        .arg("--failure-mode")
        .arg(failure_mode)
        .arg("--pause-for-kill")
        .arg(pause_marker)
        .spawn()
        .expect("spawn postgres-postgres run with failpoint armed")
}

/// Runs the real compiled binary to completion (or typed failure) with
/// fault injection armed but no `--pause-for-kill` -- the failpoint returns
/// a typed graceful error instead of pausing, so this call returns once the
/// process has actually exited on its own. Used by the pre-commit typed
/// rollback tests (never a hard kill).
#[allow(clippy::too_many_arguments)]
pub fn run_with_typed_failpoint(
    reader_mode: &str,
    import_name: &str,
    chunk_size: u32,
    size_flag: (&str, usize),
    fail_at_chunk: u32,
    failure_mode: &str,
) -> Output {
    bin()
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(chunk_size.to_string())
        .arg("--reader")
        .arg(reader_mode)
        .arg(size_flag.0)
        .arg(size_flag.1.to_string())
        .arg("--fail-at-chunk")
        .arg(fail_at_chunk.to_string())
        .arg("--failure-mode")
        .arg(failure_mode)
        .output()
        .expect("spawn postgres-postgres run with typed failpoint")
}

/// A plain `run` invocation (no fault injection), returned without
/// asserting success -- for callers that expect (and must observe) a
/// rejection, e.g. a plain restart against an execution the framework still
/// considers in-progress.
pub fn run_plain(
    reader_mode: &str,
    import_name: &str,
    chunk_size: u32,
    size_flag: (&str, usize),
) -> Output {
    bin()
        .arg("run")
        .arg("--import-name")
        .arg(import_name)
        .arg("--chunk-size")
        .arg(chunk_size.to_string())
        .arg("--reader")
        .arg(reader_mode)
        .arg(size_flag.0)
        .arg(size_flag.1.to_string())
        .output()
        .expect("spawn postgres-postgres run")
}

/// Polls `destination_row_count` until it reaches `expected` or `timeout`
/// elapses, then returns whatever the last observed count was. Guards
/// against PostgreSQL's own (fast, but not instantaneous relative to
/// `Child::wait()` returning) rollback of a hard-killed backend's abandoned
/// transaction -- polling for the real converged value, never a fixed sleep
/// guess.
pub async fn wait_for_row_count(
    pool: &PgPool,
    import_name: &str,
    source_digest: &str,
    expected: i64,
    timeout: Duration,
) -> i64 {
    let deadline = Instant::now() + timeout;
    loop {
        let count = destination_row_count(pool, import_name, source_digest).await;
        if count == expected || Instant::now() >= deadline {
            return count;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Polls (never a fixed sleep-and-hope) for `marker_path` to appear with
/// nonempty content, up to `timeout`, and returns the PID the failpoint
/// wrote into it -- independent, file-based proof of exactly which process
/// paused, on top of `Child::id()`. Panics with a clear diagnostic if the
/// failpoint never fires within `timeout`, rather than silently proceeding
/// against database state the target chunk never actually reached.
pub fn wait_for_marker(marker_path: &Path, timeout: Duration) -> u32 {
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(contents) = std::fs::read_to_string(marker_path) {
            if let Some(first_line) = contents.lines().next() {
                if let Ok(pid) = first_line.trim().parse::<u32>() {
                    return pid;
                }
            }
        }
        assert!(
            Instant::now() < deadline,
            "failpoint marker file {} never appeared within {timeout:?} -- the target chunk's \
             write phase was never reached",
            marker_path.display()
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Sends a real `SIGKILL` (via `Child::kill`, never `Result::Err`, a
/// graceful shutdown, or a self-inflicted `abort()`) to `child` and reaps
/// it, returning the resulting `ExitStatus` as crash evidence.
pub fn kill_and_wait(child: &mut Child) -> std::process::ExitStatus {
    child.kill().expect("deliver SIGKILL to paused child");
    child.wait().expect("reap killed child")
}

/// `recover --import-name <name> --reader <mode>` against the real
/// compiled binary -- the public OxideBatch recovery/operator API,
/// exercised only through this workload's own CLI, never by mutating
/// `oxide_batch` metadata directly from a test.
pub fn recover(import_name: &str, reader_mode: &str) -> Output {
    bin()
        .arg("recover")
        .arg("--import-name")
        .arg(import_name)
        .arg("--reader")
        .arg(reader_mode)
        .output()
        .expect("spawn postgres-postgres recover")
}

/// `(status, commit_count, checkpoint position)` for the latest attempt's
/// single step execution -- read-only introspection of framework-owned
/// durable metadata (`oxide_batch.ob_step_execution`'s `checkpoint_payload`
/// literally is `job::state_provider`'s `{"position": N}` shape), used only
/// to *observe* durable facts, never to construct or mutate them (see this
/// module's own doc comment and the crate README's recovery-evidence
/// section on why production code and tests alike never write to
/// `oxide_batch` directly).
pub async fn latest_checkpoint(pool: &PgPool, job_name: &str) -> Option<(String, i64, i64)> {
    sqlx::query_as::<_, (String, i64, i64)>(
        "SELECT e.status, s.commit_count, (s.checkpoint_payload->>'position')::bigint \
         FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         JOIN oxide_batch.ob_step_execution s ON s.job_execution_id = e.id \
         WHERE i.job_name = $1 \
         ORDER BY e.attempt DESC LIMIT 1",
    )
    .bind(job_name)
    .fetch_optional(pool)
    .await
    .expect("query latest checkpoint")
}

/// Number of durable `JobExecution` attempts recorded for `job_name`,
/// across every `JobInstance` sharing that name -- used to confirm recovery
/// continuation is tracked as a genuinely new execution lifecycle, not an
/// overwrite of the crashed attempt's own history.
pub async fn job_execution_count(pool: &PgPool, job_name: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT COUNT(*) FROM oxide_batch.ob_job_execution e \
         JOIN oxide_batch.ob_job_instance i ON i.id = e.job_instance_id \
         WHERE i.job_name = $1",
    )
    .bind(job_name)
    .fetch_one(pool)
    .await
    .expect("count job executions")
}

/// A deterministic destination content digest over one import's whole
/// `(display_name, loyalty_score, is_premium, row_fingerprint)` projection,
/// ordered by `customer_id` -- used to confirm a clean run and a
/// crash+recover+restart run of the identical source content converge to
/// representation-identical final business state, not merely the same row
/// count.
pub async fn destination_content_digest(
    pool: &PgPool,
    import_name: &str,
    source_digest: &str,
) -> String {
    use sha2::{Digest, Sha256};
    let rows = full_projection_rows(pool, import_name, source_digest).await;
    let mut hasher = Sha256::new();
    for (customer_id, display_name, loyalty_score, is_premium, row_fingerprint) in &rows {
        hasher.update(customer_id.to_le_bytes());
        hasher.update(display_name.as_bytes());
        hasher.update([0u8]);
        hasher.update(loyalty_score.to_le_bytes());
        hasher.update([u8::from(*is_premium)]);
        hasher.update(row_fingerprint);
        hasher.update([0xFFu8]);
    }
    postgres_postgres::hex::hex_digest(&hasher.finalize())
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

/// `(customer_id, display_name, loyalty_score, is_premium, row_fingerprint)`
/// -- the complete business projection, not just a count. Ordered by
/// `customer_id` so two calls (e.g. one per reader mode, in
/// `tests/reader_parity.rs`) can be compared element-wise.
pub type ProjectionRow = (i64, String, i64, bool, Vec<u8>);

pub async fn full_projection_rows(
    pool: &PgPool,
    import_name: &str,
    source_digest: &str,
) -> Vec<ProjectionRow> {
    sqlx::query_as::<_, ProjectionRow>(
        "SELECT customer_id, display_name, loyalty_score, is_premium, row_fingerprint \
         FROM app_business.customer_projection \
         WHERE import_name = $1 AND source_digest = $2 \
         ORDER BY customer_id",
    )
    .bind(import_name)
    .bind(source_digest)
    .fetch_all(pool)
    .await
    .expect("query full projection rows")
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

/// Every `JobInstance`'s own persisted `identifying_parameters` (framework
/// metadata, `oxide_batch.ob_job_instance`) for `job_name`, ordered by
/// creation. Test-only introspection: production code
/// (`src/job.rs`/`src/verify.rs`) never reads this table -- see
/// `tests/reader_mode_identity.rs`, which uses this to black-box-confirm
/// `reader_mode` is actually persisted as an identifying parameter, not
/// merely passed to `JobParameters` and silently dropped by the framework.
pub async fn job_instance_identifying_parameters(
    pool: &PgPool,
    job_name: &str,
) -> Vec<serde_json::Value> {
    sqlx::query_scalar::<_, serde_json::Value>(
        "SELECT identifying_parameters FROM oxide_batch.ob_job_instance \
         WHERE job_name = $1 ORDER BY id",
    )
    .bind(job_name)
    .fetch_all(pool)
    .await
    .expect("query job instance identifying_parameters")
}
