use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};

static NONCE: AtomicU64 = AtomicU64::new(1);
const MARKER_TIMEOUT: Duration = Duration::from_secs(30);
const SETTLE_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Serialize)]
struct InternalRow {
    source_row_id: String,
    merchant_payment_id: String,
    provider_payment_id: String,
    currency: String,
    normalized_status: String,
    authorized_amount_minor: i64,
    captured_amount_minor: i64,
    refunded_amount_minor: i64,
}

#[derive(Clone, Serialize)]
struct ProviderRow {
    source_row_id: String,
    provider_payment_id: String,
    currency: String,
    normalized_status: String,
    authorized_amount_minor: i64,
    captured_amount_minor: i64,
    refunded_amount_minor: i64,
    provider_version: i64,
}

#[derive(Debug, Deserialize)]
struct VerifyEnvelope {
    schema_version: u32,
    campaign_issue: u32,
    classification: String,
    snapshot: serde_json::Value,
    oracle: serde_json::Value,
    actual: serde_json::Value,
    violations: Vec<String>,
}

fn verify_report(snapshot_id: &str) -> VerifyEnvelope {
    let result = output({
        let mut command = bin();
        command.arg("verify").arg("--snapshot-id").arg(snapshot_id);
        command
    });
    assert_success(&result, "verify evidence precondition");
    serde_json::from_slice(&result.stdout).expect("parse producer evidence")
}

fn retain_evidence(
    scenario: &str,
    source_kind: &str,
    snapshot_id: &str,
    failure_point: Option<serde_json::Value>,
    observations: serde_json::Value,
) {
    let Some(directory) = std::env::var_os("PAYMENT_RECONCILIATION_EVIDENCE_DIR") else {
        return;
    };
    let report = verify_report(snapshot_id);
    assert_eq!(report.schema_version, 1);
    assert_eq!(report.campaign_issue, 148);
    assert!(
        report.violations.is_empty(),
        "producer verifier must be clean"
    );

    let value = json!({
        "schema_version": 1,
        "campaign_issue": 148,
        "classification": report.classification,
        "scenario": scenario,
        "source_kind": source_kind,
        "failure_point": failure_point,
        "observations": observations,
        "producer_report": {
            "schema_version": report.schema_version,
            "campaign_issue": report.campaign_issue,
            "snapshot": report.snapshot,
            "oracle": report.oracle,
            "actual": report.actual,
            "violations": report.violations
        }
    });

    let directory = PathBuf::from(directory);
    std::fs::create_dir_all(&directory).expect("create evidence directory");
    let path = directory.join(format!("{scenario}.json"));
    std::fs::write(
        path,
        serde_json::to_vec_pretty(&value).expect("serialize retained evidence"),
    )
    .expect("write retained evidence");
}

fn database_url() -> String {
    std::env::var("DATABASE_URL").unwrap_or_else(|_| {
        "postgresql://oxide_batch_workload:oxide_batch_workload@127.0.0.1:5436/payment_reconciliation_workload".into()
    })
}

fn unique(label: &str) -> String {
    format!(
        "{label}-{}-{}",
        std::process::id(),
        NONCE.fetch_add(1, Ordering::SeqCst)
    )
}

fn marker(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "payment-reconciliation-{label}-{}.marker",
        unique("m")
    ))
}

fn bin() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_payment-reconciliation-postgres"));
    command.env("DATABASE_URL", database_url());
    command
}

fn output(mut command: Command) -> Output {
    command
        .output()
        .expect("spawn payment-reconciliation-postgres")
}

fn assert_success(output: &Output, label: &str) {
    assert!(
        output.status.success(),
        "{label} failed: status={:?}\nstdout={}\nstderr={}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

async fn pool() -> PgPool {
    PgPool::connect(&database_url())
        .await
        .expect("connect test postgres")
}

fn digest<T: Serialize + Clone>(rows: &[(String, T)]) -> String {
    let mut rows = rows.to_vec();
    rows.sort_by(|left, right| left.0.cmp(&right.0));
    let mut hasher = Sha256::new();
    for (_, row) in rows {
        hasher.update(serde_json::to_vec(&row).expect("serialize source row"));
        hasher.update(b"\n");
    }
    format!("{:x}", hasher.finalize())
}

async fn migrate() {
    let result = output({
        let mut command = bin();
        command.arg("migrate");
        command
    });
    assert_success(&result, "migrate");
}

async fn insert_clear_snapshot(row_count: usize) -> String {
    migrate().await;
    let snapshot_id = unique("clear");
    let scope = unique("merchant");
    let mut internal = Vec::new();
    let mut provider = Vec::new();

    for index in 0..row_count {
        let provider_id = format!("{snapshot_id}-provider-{index:03}");
        internal.push(InternalRow {
            source_row_id: format!("internal:{index:03}"),
            merchant_payment_id: format!("{snapshot_id}-merchant-{index:03}"),
            provider_payment_id: provider_id.clone(),
            currency: "USD".into(),
            normalized_status: "CAPTURED".into(),
            authorized_amount_minor: 100 + index as i64,
            captured_amount_minor: 100 + index as i64,
            refunded_amount_minor: 0,
        });
        provider.push(ProviderRow {
            source_row_id: format!("provider:{index:03}"),
            provider_payment_id: provider_id,
            currency: "USD".into(),
            normalized_status: "CAPTURED".into(),
            authorized_amount_minor: 100 + index as i64,
            captured_amount_minor: 100 + index as i64,
            refunded_amount_minor: 0,
            provider_version: 1,
        });
    }

    let internal_digest = digest(
        &internal
            .iter()
            .cloned()
            .map(|row| (row.source_row_id.clone(), row))
            .collect::<Vec<_>>(),
    );
    let provider_digest = digest(
        &provider
            .iter()
            .cloned()
            .map(|row| (row.source_row_id.clone(), row))
            .collect::<Vec<_>>(),
    );

    let pool = pool().await;
    let mut tx = pool.begin().await.expect("begin snapshot insert");
    sqlx::query(
        "INSERT INTO payment_reconciliation.source_snapshot
         (snapshot_id, contract_version, business_date, provider_scope, scenario_case,
          base_snapshot_id, internal_snapshot_digest, provider_snapshot_digest,
          policy_version, mutation_recipe)
         VALUES ($1, 'payment-reconciliation-v1', '2026-11-14'::date, $2, 'CLEAR',
                 NULL, $3, $4, 'payment-reconciliation-policy-v1', '{}'::jsonb)",
    )
    .bind(&snapshot_id)
    .bind(&scope)
    .bind(&internal_digest)
    .bind(&provider_digest)
    .execute(&mut *tx)
    .await
    .expect("insert source snapshot");

    for row in &internal {
        sqlx::query(
            "INSERT INTO payment_reconciliation.internal_payment_snapshot
             (snapshot_id, source_row_id, merchant_payment_id, provider_payment_id,
              currency, normalized_status, authorized_amount_minor,
              captured_amount_minor, refunded_amount_minor)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        )
        .bind(&snapshot_id)
        .bind(&row.source_row_id)
        .bind(&row.merchant_payment_id)
        .bind(&row.provider_payment_id)
        .bind(&row.currency)
        .bind(&row.normalized_status)
        .bind(row.authorized_amount_minor)
        .bind(row.captured_amount_minor)
        .bind(row.refunded_amount_minor)
        .execute(&mut *tx)
        .await
        .expect("insert internal row");
    }

    for row in &provider {
        sqlx::query(
            "INSERT INTO payment_reconciliation.provider_payment_snapshot
             (snapshot_id, source_row_id, provider_payment_id, currency,
              normalized_status, authorized_amount_minor, captured_amount_minor,
              refunded_amount_minor, provider_version)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        )
        .bind(&snapshot_id)
        .bind(&row.source_row_id)
        .bind(&row.provider_payment_id)
        .bind(&row.currency)
        .bind(&row.normalized_status)
        .bind(row.authorized_amount_minor)
        .bind(row.captured_amount_minor)
        .bind(row.refunded_amount_minor)
        .bind(row.provider_version)
        .execute(&mut *tx)
        .await
        .expect("insert provider row");
    }
    tx.commit().await.expect("commit snapshot");
    pool.close().await;
    snapshot_id
}

fn run(snapshot_id: &str) -> Output {
    output({
        let mut command = bin();
        command
            .arg("run")
            .arg("--snapshot-id")
            .arg(snapshot_id)
            .arg("--chunk-size")
            .arg("2")
            .arg("--page-size")
            .arg("2");
        command
    })
}

fn spawn_crashing_run(snapshot_id: &str, failure_mode: &str, marker: &Path) -> Child {
    bin()
        .arg("run")
        .arg("--snapshot-id")
        .arg(snapshot_id)
        .arg("--chunk-size")
        .arg("2")
        .arg("--page-size")
        .arg("2")
        .arg("--fail-at-chunk")
        .arg("2")
        .arg("--failure-mode")
        .arg(failure_mode)
        .arg("--pause-for-kill")
        .arg(marker)
        .spawn()
        .expect("spawn crashing run")
}

fn spawn_after_processing_commit(snapshot_id: &str, marker: &Path) -> Child {
    bin()
        .arg("run")
        .arg("--snapshot-id")
        .arg(snapshot_id)
        .arg("--chunk-size")
        .arg("2")
        .arg("--page-size")
        .arg("2")
        .arg("--pause-after-processing-commit")
        .arg(marker)
        .spawn()
        .expect("spawn post-processing crash run")
}

fn recover(snapshot_id: &str) -> Output {
    output({
        let mut command = bin();
        command.arg("recover").arg("--snapshot-id").arg(snapshot_id);
        command
    })
}

fn finalize(snapshot_id: &str) -> Output {
    output({
        let mut command = bin();
        command
            .arg("finalize")
            .arg("--snapshot-id")
            .arg(snapshot_id);
        command
    })
}

fn spawn_crashing_finalize(snapshot_id: &str, crash_point: &str, marker: &Path) -> Child {
    bin()
        .arg("finalize")
        .arg("--snapshot-id")
        .arg(snapshot_id)
        .arg("--finalize-crash-point")
        .arg(crash_point)
        .arg("--pause-for-kill")
        .arg(marker)
        .spawn()
        .expect("spawn crashing finalize")
}

fn wait_marker(path: &Path) -> u32 {
    let deadline = Instant::now() + MARKER_TIMEOUT;
    loop {
        if let Ok(contents) = std::fs::read_to_string(path) {
            if let Some(pid) = contents
                .lines()
                .next()
                .and_then(|line| line.parse::<u32>().ok())
            {
                return pid;
            }
        }
        assert!(
            Instant::now() < deadline,
            "marker {} did not appear within {:?}",
            path.display(),
            MARKER_TIMEOUT
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn kill(child: &mut Child) {
    child.kill().expect("kill paused child");
    let status = child.wait().expect("reap killed child");
    assert!(!status.success(), "hard-killed child must not succeed");
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        assert_eq!(status.signal(), Some(9), "child must receive SIGKILL");
    }
}

async fn detail_count(snapshot_id: &str) -> i64 {
    let pool = pool().await;
    let count = sqlx::query_scalar(
        "SELECT COUNT(*)::BIGINT
         FROM payment_reconciliation.reconciliation_detail d
         JOIN payment_reconciliation.reconciliation_run r ON r.run_id = d.run_id
         WHERE r.snapshot_id = $1",
    )
    .bind(snapshot_id)
    .fetch_one(&pool)
    .await
    .expect("count detail");
    pool.close().await;
    count
}

async fn wait_detail_count(snapshot_id: &str, expected: i64) -> i64 {
    let deadline = Instant::now() + SETTLE_TIMEOUT;
    loop {
        let current = detail_count(snapshot_id).await;
        if current == expected || Instant::now() >= deadline {
            return current;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

async fn run_state(snapshot_id: &str) -> (String, Option<String>, i64) {
    let pool = pool().await;
    let row = sqlx::query(
        "SELECT r.processing_state, r.business_state,
                (SELECT COUNT(*)::BIGINT
                   FROM payment_reconciliation.published_reconciliation p
                  WHERE p.run_id = r.run_id) AS published_count
         FROM payment_reconciliation.reconciliation_run r
         WHERE r.snapshot_id = $1",
    )
    .bind(snapshot_id)
    .fetch_one(&pool)
    .await
    .expect("load run state");
    let result = (
        row.try_get("processing_state").expect("processing state"),
        row.try_get("business_state").expect("business state"),
        row.try_get("published_count").expect("published count"),
    );
    pool.close().await;
    result
}

async fn derive_amount_mismatch(base: &str) -> String {
    let derived = unique("held");
    let result = output({
        let mut command = bin();
        command
            .arg("derive-scenario")
            .arg("--base-snapshot-id")
            .arg(base)
            .arg("--snapshot-id")
            .arg(&derived)
            .arg("--scenario")
            .arg("amount-mismatch")
            .arg("--delta-minor")
            .arg("1");
        command
    });
    assert_success(&result, "derive amount mismatch");
    derived
}

#[tokio::test]
async fn hard_crash_during_chunk_write_rolls_back_and_recovers_in_new_process() {
    let snapshot = insert_clear_snapshot(5).await;
    let path = marker("during-write");
    let mut child = spawn_crashing_run(&snapshot, "during-write", &path);
    assert_eq!(wait_marker(&path), child.id());
    kill(&mut child);

    assert_eq!(wait_detail_count(&snapshot, 2).await, 2);
    let plain_restart = run(&snapshot);
    assert!(
        !plain_restart.status.success(),
        "plain restart before explicit recovery must be rejected"
    );
    let recovery = recover(&snapshot);
    assert_success(&recovery, "recover before-commit crash");
    assert_success(&run(&snapshot), "restart after before-commit crash");
    assert_eq!(detail_count(&snapshot).await, 5);
    assert_success(&finalize(&snapshot), "finalize recovered run");
    assert_eq!(
        run_state(&snapshot).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );
    retain_evidence(
        "hard-crash-during-write",
        "deterministic-frozen-recovery-fixture",
        &snapshot,
        Some(json!({"boundary":"chunk-during-write","target_chunk":2,"signal":9})),
        json!({
            "detail_count_after_kill": 2,
            "plain_restart_rejected": true,
            "explicit_public_recovery": true,
            "new_process_continuation": true,
            "final_publication_count": 1
        }),
    );
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn hard_crash_after_chunk_commit_preserves_checkpoint_and_avoids_duplicate_delivery() {
    let snapshot = insert_clear_snapshot(5).await;
    let path = marker("after-commit");
    let mut child = spawn_crashing_run(&snapshot, "after-business-commit", &path);
    assert_eq!(wait_marker(&path), child.id());
    kill(&mut child);

    assert_eq!(wait_detail_count(&snapshot, 4).await, 4);
    let plain_restart = run(&snapshot);
    assert!(
        !plain_restart.status.success(),
        "plain restart before explicit recovery must be rejected"
    );
    let recovery = recover(&snapshot);
    assert_success(&recovery, "recover after-commit crash");
    assert_success(&run(&snapshot), "restart after committed chunk crash");
    assert_eq!(detail_count(&snapshot).await, 5);
    assert_success(&finalize(&snapshot), "finalize after recovery");
    assert_eq!(
        run_state(&snapshot).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );
    retain_evidence(
        "hard-crash-after-chunk-commit",
        "deterministic-frozen-recovery-fixture",
        &snapshot,
        Some(json!({"boundary":"chunk-after-business-commit","target_chunk":2,"signal":9})),
        json!({
            "detail_count_after_kill": 4,
            "plain_restart_rejected": true,
            "explicit_public_recovery": true,
            "new_process_continuation": true,
            "final_publication_count": 1
        }),
    );
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn process_death_after_processing_commit_still_publishes_nothing_until_new_process_finalizes()
{
    let snapshot = insert_clear_snapshot(3).await;
    let path = marker("after-processing");
    let mut child = spawn_after_processing_commit(&snapshot, &path);
    assert_eq!(wait_marker(&path), child.id());
    kill(&mut child);

    assert_eq!(run_state(&snapshot).await, ("PROCESSED".into(), None, 0));

    let result = finalize(&snapshot);
    assert_success(
        &result,
        "separate-process finalization after processing-process death",
    );
    assert_eq!(
        run_state(&snapshot).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );
    retain_evidence(
        "hard-crash-after-processing-commit",
        "deterministic-frozen-recovery-fixture",
        &snapshot,
        Some(json!({"boundary":"after-processing-commit-before-finalization","signal":9})),
        json!({
            "publication_count_after_kill": 0,
            "business_state_after_kill": null,
            "separate_process_finalization": true,
            "final_publication_count": 1
        }),
    );
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn hard_crash_before_finalize_commit_leaves_no_publication_and_replay_converges() {
    let snapshot = insert_clear_snapshot(3).await;
    assert_success(&run(&snapshot), "clean processing run");

    let path = marker("finalize-before");
    let mut child = spawn_crashing_finalize(&snapshot, "before-commit", &path);
    assert_eq!(wait_marker(&path), child.id());
    kill(&mut child);

    assert_eq!(run_state(&snapshot).await, ("PROCESSED".into(), None, 0));
    assert_success(&finalize(&snapshot), "replay finalize after rollback");
    assert_eq!(
        run_state(&snapshot).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );
    retain_evidence(
        "hard-crash-before-finalize-commit",
        "deterministic-frozen-recovery-fixture",
        &snapshot,
        Some(json!({"boundary":"finalize-before-commit","signal":9})),
        json!({
            "publication_count_after_kill": 0,
            "business_state_after_kill": null,
            "finalize_replay": true,
            "final_publication_count": 1
        }),
    );
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn hard_crash_after_publication_commit_replays_without_duplicate_publication() {
    let snapshot = insert_clear_snapshot(3).await;
    assert_success(&run(&snapshot), "clean processing run");

    let path = marker("finalize-after");
    let mut child = spawn_crashing_finalize(&snapshot, "after-commit", &path);
    assert_eq!(wait_marker(&path), child.id());
    kill(&mut child);

    assert_eq!(
        run_state(&snapshot).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );
    assert_success(
        &finalize(&snapshot),
        "replay finalize after committed response loss",
    );
    assert_eq!(
        run_state(&snapshot).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );
    retain_evidence(
        "hard-crash-after-publication-commit",
        "deterministic-frozen-recovery-fixture",
        &snapshot,
        Some(json!({"boundary":"finalize-after-commit-before-ack","signal":9})),
        json!({
            "publication_count_after_kill": 1,
            "business_state_after_kill": "CLEAR",
            "finalize_replay": true,
            "final_publication_count": 1
        }),
    );
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn clear_and_held_finalization_replay_preserve_publication_cardinality() {
    let clear = insert_clear_snapshot(3).await;
    assert_success(&run(&clear), "clear run");
    assert_success(&finalize(&clear), "clear finalize one");
    assert_success(&finalize(&clear), "clear finalize replay");
    assert_eq!(
        run_state(&clear).await,
        ("PROCESSED".into(), Some("CLEAR".into()), 1)
    );

    let base = insert_clear_snapshot(3).await;
    let held = derive_amount_mismatch(&base).await;
    assert_success(&run(&held), "held run");
    assert_success(&finalize(&held), "held finalize one");
    assert_success(&finalize(&held), "held finalize replay");
    assert_eq!(
        run_state(&held).await,
        ("PROCESSED".into(), Some("HELD".into()), 0)
    );
    retain_evidence(
        "clear-finalize-replay",
        "deterministic-frozen-recovery-fixture",
        &clear,
        None,
        json!({"finalize_replays":2,"final_publication_count":1}),
    );
    retain_evidence(
        "held-finalize-replay",
        "deterministic-frozen-recovery-fixture",
        &held,
        None,
        json!({"finalize_replays":2,"final_publication_count":0}),
    );
}
