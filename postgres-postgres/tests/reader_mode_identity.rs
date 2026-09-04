//! Mode identity isolation (campaign #63, PR 2): `reader_mode` is an
//! identifying `JobParameter` (see `src/job.rs::parameters`), so the exact
//! same `import_name` against the exact same source content, run once under
//! `--reader cursor` and once under `--reader paging`, must resolve to two
//! distinct `JobInstance`s -- never one instance that the second run
//! silently resumes (which would mean paging inherits/reinterprets cursor's
//! persisted keyset-position state, or vice versa; see `src/job.rs`'s
//! module documentation).
//!
//! This is the black-box proof that reader mode actually participates in
//! identifying state: it observes framework-owned durable metadata
//! (`oxide_batch.ob_job_instance`) directly, not anything this workload's
//! own production code path exposes.
//!
//! # Why this test truncates `app_business.customer_projection` between runs
//!
//! `app_business.customer_projection`'s primary key is `(import_name,
//! source_digest, customer_id)` (see `migrations/001_init.sql`) --
//! deliberately *not* including `reader_mode`, because business output
//! scoping and job-instance identity/resumability are different concerns
//! (see `src/job.rs::parameters`'s doc comment). That means two runs
//! sharing one `import_name` and one `source_digest` -- exactly this test's
//! setup -- write into the *same* destination scope. Running the second
//! (paging) attempt straight after the first (cursor) without clearing that
//! shared scope would collide on the primary key and fail the run for a
//! reason unrelated to the property under test. This test truncates only
//! `app_business.customer_projection` (never `app_source.source_customer`)
//! between the two runs, directly via SQL -- workload-owned business data,
//! same category of direct test manipulation
//! `tests/verifier_negative_control.rs` already performs -- so the second
//! run starts from a clean destination while both runs still observe the
//! exact same, untouched source content and therefore the exact same
//! `source_digest`.

mod support;

use support::SeedOptions;

#[tokio::test]
async fn cursor_and_paging_under_the_same_import_name_and_source_resolve_to_distinct_instances() {
    support::migrate();
    support::reset();
    let dataset = support::seed(SeedOptions {
        rows: 120,
        seed: 63,
    });

    // Same import_name for both runs is the entire point of this test: if
    // reader_mode were not identifying, the second run below would resolve
    // to the same JobInstance the first run created and either resume it
    // (misinterpreting the other reader's persisted stream state) or
    // conflict with it -- neither of which is what actually happens.
    let import_name = support::unique_name("mode_identity");

    let pool = support::pool().await;

    support::run_cursor(&import_name, 40);
    let source_digest = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest after the cursor run");
    let cursor_destination_rows =
        support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(cursor_destination_rows, dataset.rows as i64);
    let verify_cursor = support::verify(&import_name);
    assert!(
        verify_cursor.status.success(),
        "cursor run must verify cleanly: {}",
        String::from_utf8_lossy(&verify_cursor.stderr)
    );

    // See the module documentation above for why only the business table
    // (never app_source) is cleared here.
    sqlx::raw_sql("TRUNCATE TABLE app_business.customer_projection")
        .execute(&pool)
        .await
        .expect("truncate the shared destination scope before the paging run");

    support::run_paging(&import_name, 40);
    let source_digest_after_paging = postgres_postgres::source_digest::compute(&pool)
        .await
        .expect("compute source digest after the paging run");
    assert_eq!(
        source_digest, source_digest_after_paging,
        "app_source was never touched between the two runs, so both must have observed \
         identical source content"
    );
    let paging_destination_rows =
        support::destination_row_count(&pool, &import_name, &source_digest).await;
    assert_eq!(paging_destination_rows, dataset.rows as i64);
    let verify_paging = support::verify(&import_name);
    assert!(
        verify_paging.status.success(),
        "paging run must verify cleanly: {}",
        String::from_utf8_lossy(&verify_paging.stderr)
    );

    // The actual property under test: two distinct JobInstances, not one
    // shared/resumed instance, despite the identical import_name and
    // source_digest above.
    let instance_count = support::job_instance_count(&pool, &import_name).await;
    assert_eq!(
        instance_count, 2,
        "cursor and paging under the same import_name and exact same source content must \
         resolve to two distinct OxideBatch JobInstances, not one shared/resumed instance"
    );

    // Framework metadata cleanly exposes which parameters were identifying
    // for each instance (oxide_batch.ob_job_instance.identifying_parameters
    // -- see tests/support/mod.rs::job_instance_identifying_parameters);
    // assert reader_mode is actually present there, in both instances, with
    // the two different values -- test-only introspection, never depended
    // on by production code.
    let identifying = support::job_instance_identifying_parameters(&pool, &import_name).await;
    assert_eq!(identifying.len(), 2);
    let reader_modes: std::collections::BTreeSet<String> = identifying
        .iter()
        .map(|params| {
            let entry = &params["reader_mode"];
            assert_eq!(
                entry["identifying"].as_bool(),
                Some(true),
                "reader_mode must itself be recorded with role identifying=true, not merely present"
            );
            entry["value"]
                .as_str()
                .expect("reader_mode has a string value in identifying_parameters")
                .to_owned()
        })
        .collect();
    assert_eq!(
        reader_modes,
        std::collections::BTreeSet::from(["cursor".to_owned(), "paging".to_owned()]),
        "both instances' identifying_parameters must record their own distinct reader_mode value"
    );
}
