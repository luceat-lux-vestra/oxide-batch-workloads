use std::env;
use std::fs;
use std::process::Command;

#[test]
fn paired_benchmark_harness_contract_and_database_smoke_pass() {
    let unit_status = Command::new("python3")
        .arg("benchmark/test_paired.py")
        .status()
        .expect("python3 must execute paired benchmark unit tests");
    assert!(unit_status.success(), "paired benchmark unit tests failed");

    let Ok(database_url) = env::var("POSTGRES_POSTGRES_TEST_DATABASE_URL") else {
        eprintln!(
            "skipping paired benchmark database smoke: POSTGRES_POSTGRES_TEST_DATABASE_URL not set"
        );
        return;
    };

    let output = env::temp_dir().join(format!(
        "postgres-postgres-paired-smoke-{}.json",
        std::process::id()
    ));
    let status = Command::new("python3")
        .arg("benchmark/paired.py")
        .args([
            "--oxide-binary",
            "target/debug/postgres-postgres",
            "--raw-binary",
            "target/debug/raw-sqlx",
            "--base-database-url",
            &database_url,
            "--rows",
            "401",
            "--seed",
            "42",
            "--chunk-size",
            "100",
            "--fetch-size",
            "60",
            "--page-size",
            "70",
            "--warmups",
            "0",
            "--measured-runs",
            "1",
            "--output",
        ])
        .arg(&output)
        .status()
        .expect("python3 must execute paired benchmark database smoke");

    let report_text = fs::read_to_string(&output).expect("paired smoke report must exist");
    let _ = fs::remove_file(&output);
    let report: serde_json::Value =
        serde_json::from_str(&report_text).expect("paired smoke report must be valid JSON");

    assert!(
        status.success(),
        "paired database smoke failed: {report_text}"
    );
    assert_eq!(report["status"], "passed");
    assert_eq!(
        report["clean"]["cursor"]["measured_pairs"]
            .as_array()
            .map(Vec::len),
        Some(1)
    );
    assert_eq!(
        report["clean"]["paging"]["measured_pairs"]
            .as_array()
            .map(Vec::len),
        Some(1)
    );
    assert_eq!(
        report["recovery"]["cursor"]["candidates"]["oxide"]["verification"]["total_mismatches"],
        0
    );
    assert_eq!(
        report["recovery"]["cursor"]["candidates"]["raw"]["verification"]["total_mismatches"],
        0
    );
    assert_eq!(
        report["recovery"]["paging"]["candidates"]["oxide"]["verification"]["total_mismatches"],
        0
    );
    assert_eq!(
        report["recovery"]["paging"]["candidates"]["raw"]["verification"]["total_mismatches"],
        0
    );
}
