//! Source content identity (spec: "Source identity"). `job::run` computes
//! a streaming digest over `app_source.source_customer` in canonical
//! `customer_id` order and uses it, alongside the user-facing import name,
//! as an identifying `JobParameter` -- see `src/source_digest.rs` and
//! `src/job.rs`.
//!
//! PR 1 proves the clean-path identity mechanism: determinism, sensitivity
//! to a changed source, and collision-free destination scoping. Full
//! mutation/resume/restart evidence is a later PR (see the crate README's
//! scope section).

mod support;

use postgres_postgres::source_digest;
use support::SeedOptions;

/// Depends on exclusive access to `app_source.source_customer` (via
/// `migrate`+`reset`-style truncation between seeds), same as
/// `clean_run.rs`'s connection-sanity test -- run under this workload's
/// documented `--test-threads=1`.
#[tokio::test]
async fn identical_seed_produces_an_identical_source_digest() {
    support::migrate();
    let pool = support::pool().await;

    // A fixed, shared id_offset (not support::seed's own nonce-derived
    // one) so both calls below produce byte-identical customer_id ranges --
    // otherwise two "identical" seed calls would deliberately generate
    // disjoint customer_id ranges and never match, which would be testing
    // the wrong thing.
    let fixed_offset = support::unique_id_offset();

    sqlx::raw_sql("TRUNCATE TABLE app_business.customer_projection; TRUNCATE TABLE app_source.source_customer")
        .execute(&pool)
        .await
        .expect("truncate workload tables");
    support::seed_at(
        SeedOptions {
            rows: 200,
            seed: 4242,
        },
        fixed_offset,
    );
    let digest_a = source_digest::compute(&pool)
        .await
        .expect("compute digest for run A");

    sqlx::raw_sql("TRUNCATE TABLE app_business.customer_projection; TRUNCATE TABLE app_source.source_customer")
        .execute(&pool)
        .await
        .expect("truncate workload tables");
    support::seed_at(
        SeedOptions {
            rows: 200,
            seed: 4242,
        },
        fixed_offset,
    );
    let digest_b = source_digest::compute(&pool)
        .await
        .expect("compute digest for run B");

    assert_eq!(
        digest_a, digest_b,
        "the same (rows, seed) must resolve to the same source content digest"
    );
}

#[tokio::test]
async fn a_changed_seed_produces_a_different_source_digest() {
    support::migrate();
    let pool = support::pool().await;

    sqlx::raw_sql("TRUNCATE TABLE app_business.customer_projection; TRUNCATE TABLE app_source.source_customer")
        .execute(&pool)
        .await
        .expect("truncate workload tables");
    support::seed(SeedOptions { rows: 200, seed: 1 });
    let digest_a = source_digest::compute(&pool)
        .await
        .expect("compute digest for seed 1");

    sqlx::raw_sql("TRUNCATE TABLE app_business.customer_projection; TRUNCATE TABLE app_source.source_customer")
        .execute(&pool)
        .await
        .expect("truncate workload tables");
    support::seed(SeedOptions { rows: 200, seed: 2 });
    let digest_b = source_digest::compute(&pool)
        .await
        .expect("compute digest for seed 2");

    assert_ne!(
        digest_a, digest_b,
        "a changed source must resolve to a distinct content identity"
    );
}

/// Two independent import names launched against the *same* current source
/// content (so both resolve to the identical `source_digest`) must still
/// write disjoint, non-colliding destination rows: proves scoping is
/// carried by the full `(import_name, source_digest, customer_id)` primary
/// key (migrations/001_init.sql), not by `source_digest` alone -- a source
/// identity match is not sufficient to let two different logical imports'
/// rows collide or overwrite each other.
#[tokio::test]
async fn two_import_names_sharing_one_source_identity_do_not_collide_in_the_destination() {
    support::migrate();
    // run/verify cover the whole app_source table; reset first so the
    // combined-content assertions below are not disturbed by other tests'
    // residual data (see tests/support/mod.rs::reset's doc comment).
    support::reset();
    // Both datasets land in app_source before either run launches, and
    // neither `run` nor `seed` ever mutates app_source afterward, so both
    // runs below observe identical source content and therefore compute
    // the identical source_digest.
    let dataset_a = support::seed(SeedOptions {
        rows: 100,
        seed: 11,
    });
    let dataset_b = support::seed(SeedOptions {
        rows: 100,
        seed: 22,
    });

    let import_a = support::unique_name("identity_a");
    support::run(&import_a, 25);
    let import_b = support::unique_name("identity_b");
    support::run(&import_b, 25);
    assert_ne!(import_a, import_b);

    let pool = support::pool().await;
    let digest = source_digest::compute(&pool)
        .await
        .expect("compute the shared source digest both runs observed");

    let count_a = support::destination_row_count(&pool, &import_a, &digest).await;
    let count_b = support::destination_row_count(&pool, &import_b, &digest).await;
    assert_eq!(
        count_a,
        dataset_a.rows as i64 + dataset_b.rows as i64,
        "import_a's own scope must contain exactly its own run's rows over the full source"
    );
    assert_eq!(
        count_b,
        dataset_a.rows as i64 + dataset_b.rows as i64,
        "import_b's own scope must contain exactly its own run's rows over the full source"
    );

    // Each import's verifier independently confirms its own scope is
    // exactly correct -- not merely that some row count happens to match.
    let verify_a = support::verify(&import_a);
    assert!(verify_a.status.success(), "import_a must verify cleanly");
    let verify_b = support::verify(&import_b);
    assert!(verify_b.status.success(), "import_b must verify cleanly");
}
