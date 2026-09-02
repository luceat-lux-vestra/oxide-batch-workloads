//! Golden-path smoke check: spawns the compiled binary as a real child
//! process (never an in-process function call) against a checked-in
//! deterministic input file, and compares its stdout byte-for-byte against a
//! checked-in golden file. This is this fixture's equivalent of
//! `csv-postgres`'s golden-path smoke test, deliberately using a completely
//! different mechanism (no services, no database, no restart semantics).

use std::path::PathBuf;
use std::process::Command;

fn fixture_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name)
}

#[test]
fn golden_path_histogram_matches_checked_in_output() {
    let output = Command::new(env!("CARGO_BIN_EXE_fixture-heterogeneous"))
        .arg("histogram")
        .arg(fixture_path("input.txt"))
        .output()
        .expect("failed to spawn fixture-heterogeneous binary");

    assert!(
        output.status.success(),
        "binary exited non-zero: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let actual = String::from_utf8(output.stdout).expect("stdout was not valid UTF-8");
    let expected =
        std::fs::read_to_string(fixture_path("golden.json")).expect("missing golden.json fixture");
    assert_eq!(
        actual, expected,
        "histogram output drifted from the checked-in golden file"
    );
}

#[test]
fn missing_input_file_fails_closed_with_nonzero_exit() {
    let output = Command::new(env!("CARGO_BIN_EXE_fixture-heterogeneous"))
        .arg("histogram")
        .arg(fixture_path("does-not-exist.txt"))
        .output()
        .expect("failed to spawn fixture-heterogeneous binary");

    assert!(
        !output.status.success(),
        "expected non-zero exit for a missing input file"
    );
}

#[test]
fn unknown_command_fails_closed_with_nonzero_exit() {
    let output = Command::new(env!("CARGO_BIN_EXE_fixture-heterogeneous"))
        .arg("not-a-real-command")
        .output()
        .expect("failed to spawn fixture-heterogeneous binary");

    assert!(
        !output.status.success(),
        "expected non-zero exit for an unknown command"
    );
}
