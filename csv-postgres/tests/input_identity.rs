//! T9: input-mutation guard (spec ss15). Job identity includes the input
//! file's SHA-256 as an *identifying* `JobParameter` (see `job::run`), so
//! the same `import_name` pointed at the *same path* whose *content*
//! changed resolves to a different `JobInstanceKey` -- a fresh, independent
//! instance -- rather than resuming the old instance's checkpoint against
//! new bytes. This test proves that mechanism end to end rather than
//! assuming it: two versions of "the same file path" import into two
//! disjoint customer_id ranges, and both land intact with two separate
//! durable job instances recorded.

mod support;

use support::{
    business_row_count_in_range, job_instance_count, latest_execution_status, GenerateOptions,
};

#[tokio::test]
async fn same_path_mutated_content_creates_a_new_instance_instead_of_resuming_the_old_checkpoint() {
    support::migrate();
    let import_name = support::unique_name("input_identity");
    let path = support::temp_csv("input-identity");

    // Version 1: write directly at `path`, run to completion.
    let v1 = support::generate(GenerateOptions {
        rows: 20,
        label: "input-identity-v1",
        ..Default::default()
    });
    std::fs::copy(&v1.path, &path).expect("seed the shared path with version 1");
    support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(&path)
            .arg("--import-name")
            .arg(&import_name)
            .arg("--chunk-size")
            .arg("100"),
    );

    let pool = support::pool().await;
    let v1_rows = business_row_count_in_range(&pool, v1.id_offset, v1.rows).await;
    assert_eq!(v1_rows, v1.rows as i64, "version 1 imported completely");
    assert_eq!(job_instance_count(&pool, &import_name).await, 1);

    // Version 2: different content (disjoint customer_id range) written to
    // the *same path*. This is a real content mutation: different SHA-256.
    let v2 = support::generate(GenerateOptions {
        rows: 20,
        seed: 4242,
        label: "input-identity-v2",
        ..Default::default()
    });
    assert_ne!(
        v1.id_offset, v2.id_offset,
        "the two versions must not share a customer_id range"
    );
    std::fs::copy(&v2.path, &path).expect("mutate the shared path to version 2's content");

    // Same import_name, same path -- but the file's bytes (and therefore its
    // SHA-256) are now different from what version 1's checkpoint was built
    // against.
    support::run_ok(
        support::bin()
            .arg("run")
            .arg("--input")
            .arg(&path)
            .arg("--import-name")
            .arg(&import_name)
            .arg("--chunk-size")
            .arg("100"),
    );

    // Two independent durable instances now exist under the same job_name:
    // the framework treated the content change as a new logical instance,
    // never as a resume of version 1's stale checkpoint against version 2's
    // bytes.
    assert_eq!(
        job_instance_count(&pool, &import_name).await,
        2,
        "content-changed input must be a new JobInstance, not a resumed one"
    );

    let v1_rows_after = business_row_count_in_range(&pool, v1.id_offset, v1.rows).await;
    assert_eq!(
        v1_rows_after, v1.rows as i64,
        "version 1's already-committed rows are untouched"
    );
    let v2_rows = business_row_count_in_range(&pool, v2.id_offset, v2.rows).await;
    assert_eq!(
        v2_rows, v2.rows as i64,
        "version 2 imported completely as its own fresh instance"
    );

    let (status, _) = latest_execution_status(&pool, &import_name)
        .await
        .expect("execution recorded");
    assert_eq!(status, "COMPLETED");

    let _ = std::fs::remove_file(&path);
}
