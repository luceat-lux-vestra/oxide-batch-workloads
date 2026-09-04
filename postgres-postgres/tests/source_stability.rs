//! Adversarial proof that the source-stability guard
//! (`source_digest::lock_source_for_stable_read`, see its module
//! documentation in `src/source_digest.rs`) actually closes the
//! time-of-check-to-time-of-use window between computing a `source_digest`
//! and the cursor reader's later, separately connected read of that same
//! source -- not merely "usually fine in practice."
//!
//! This test does not assume the guard works because the code looks right;
//! it attacks the window directly: while a real `run` is in flight, it
//! confirms (via `pg_locks`) the guard's `ShareLock` is actually held, then
//! attempts a real concurrent `INSERT` into `app_source.source_customer`
//! from an independent connection and proves that write is genuinely
//! blocked (a `lock_timeout` failure, PostgreSQL SQLSTATE `55P03`) for as
//! long as the run holds the guard -- and that the same write succeeds
//! immediately once the run has finished and released it.

mod support;

use std::time::{Duration, Instant};

use sqlx::PgPool;

/// Polls `pg_locks` for a granted `ShareLock` on
/// `app_source.source_customer`, returning once observed. Panics if the
/// lock is never observed within `timeout` -- either the guard failed to
/// engage, or the background run finished before this could catch it (in
/// which case the dataset/timing below needs to be larger/slower, not this
/// polling loop).
async fn wait_for_source_share_lock(pool: &PgPool, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        let held: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pg_locks l \
             JOIN pg_class c ON l.relation = c.oid \
             JOIN pg_namespace n ON c.relnamespace = n.oid \
             WHERE n.nspname = 'app_source' AND c.relname = 'source_customer' \
               AND l.mode = 'ShareLock' AND l.granted = true",
        )
        .fetch_one(pool)
        .await
        .expect("query pg_locks");
        if held > 0 {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "never observed the source-stability guard's ShareLock on \
             app_source.source_customer within {timeout:?} -- either the guard failed to \
             engage, or the background run finished before this test could catch it in flight"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

/// Attempts one `INSERT` into `app_source.source_customer` with a short
/// `lock_timeout`, returning the resulting `sqlx::Error` if it was
/// rejected for lock contention (`Ok` would mean the write went through,
/// which this test treats as a hard failure of the property under test).
async fn attempt_conflicting_insert(pool: &PgPool, customer_id: i64) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await.expect("begin probe transaction");
    sqlx::query("SET LOCAL lock_timeout = '800ms'")
        .execute(&mut *tx)
        .await
        .expect("set local lock_timeout on probe transaction");
    let result = sqlx::query(
        "INSERT INTO app_source.source_customer (customer_id, full_name, is_active, balance_cents) \
         VALUES ($1, 'source-stability-probe', true, 0)",
    )
    .bind(customer_id)
    .execute(&mut *tx)
    .await;
    let _ = tx.rollback().await;
    result.map(|_| ())
}

#[tokio::test]
async fn a_concurrent_write_is_blocked_while_a_run_holds_the_source_stability_guard() {
    support::migrate();
    support::reset();
    let pool = support::pool().await;

    // Large enough, at a small enough fetch/chunk size, to keep `run`
    // actually in flight (holding the guard) for long enough that this
    // test can reliably observe the lock and attempt its own conflicting
    // write inside that window -- not a fixed sleep racing against an
    // unknown-duration background process.
    let dataset = support::seed(support::SeedOptions {
        rows: 20_000,
        seed: 71,
    });
    let import_name = support::unique_name("source_stability");
    let mut child = support::spawn_run_with_fetch_size(&import_name, 100, 20);

    wait_for_source_share_lock(&pool, Duration::from_secs(15)).await;

    let probe_customer_id = (dataset.id_offset + dataset.rows + 1) as i64;
    let blocked = attempt_conflicting_insert(&pool, probe_customer_id).await;
    let status = child.wait().expect("wait for the background run to finish");

    assert!(
        blocked.is_err(),
        "a concurrent INSERT into app_source.source_customer succeeded while a run held the \
         source-stability guard -- the TOCTOU window between computing source_digest and the \
         cursor reader's actual read is NOT closed"
    );
    let sqlstate = match blocked.unwrap_err() {
        sqlx::Error::Database(db) => db.code().map(|code| code.into_owned()),
        other => panic!("expected a database error from lock contention, got: {other:?}"),
    };
    assert_eq!(
        sqlstate.as_deref(),
        Some("55P03"),
        "expected PostgreSQL's lock_not_available SQLSTATE (a lock_timeout expiry), got {sqlstate:?} \
         -- a different error would not actually demonstrate the write was blocked by the guard"
    );

    assert!(
        status.success(),
        "the background run itself must still complete successfully despite the concurrent \
         write attempt against it"
    );

    // The run's own result is not corrupted by any of the above: the
    // digest it computed under the guard is still an honest description of
    // what it actually processed. Checked *before* the next step below,
    // which deliberately mutates app_source -- verify recomputes the
    // source digest live from current content (see src/verify.rs), so it
    // must run against the exact state the run actually processed, not
    // after this test adds more rows of its own.
    let verify_output = support::verify(&import_name);
    assert!(
        verify_output.status.success(),
        "verify must still pass cleanly after the contention attempt: {}",
        String::from_utf8_lossy(&verify_output.stderr)
    );

    // The guard is released now (the run has finished); the same write
    // must succeed immediately with no lock_timeout needed, proving the
    // guard does not leak a permanent lock.
    sqlx::query(
        "INSERT INTO app_source.source_customer (customer_id, full_name, is_active, balance_cents) \
         VALUES ($1, 'source-stability-probe-after', true, 0)",
    )
    .bind(probe_customer_id)
    .execute(&pool)
    .await
    .expect("the same write must succeed once the run has released the source-stability guard");
}
