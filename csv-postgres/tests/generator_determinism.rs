//! Regression coverage for the `rand` 0.8 -> 0.10 / `rand_chacha` 0.3 ->
//! 0.10 dependency migration, and more generally for `generator::generate`'s
//! documented promise that the same `(rows, seed, variant)` always produces
//! byte-identical output (see `src/generator.rs`'s module doc comment).
//!
//! Drives the compiled `csv-postgres generate` subcommand as a real child
//! process, never an in-process function call (spec ss18 / AGENTS.md) --
//! `generate` never touches PostgreSQL, so no database is needed here.

mod support;

use std::fs;
use std::path::Path;

/// Runs `generate` with the given parameters and returns its manifest
/// sidecar (`<output>.manifest.json`), which carries the SHA-256 the CLI
/// itself computed.
fn generate_to(path: &Path, rows: u64, seed: u64, id_offset: u64) -> serde_json::Value {
    support::run_ok(
        support::bin()
            .arg("generate")
            .arg("--output")
            .arg(path)
            .arg("--rows")
            .arg(rows.to_string())
            .arg("--seed")
            .arg(seed.to_string())
            .arg("--id-offset")
            .arg(id_offset.to_string()),
    );
    let sidecar = path.with_extension("manifest.json");
    let raw = fs::read_to_string(&sidecar).expect("read generate's manifest sidecar");
    serde_json::from_str(&raw).expect("parse manifest JSON")
}

/// Two independent processes given identical `(rows, seed, id_offset)` must
/// produce byte-identical CSV files and an identical reported SHA-256 --
/// the evidence a clean run and a restart run rely on to prove they
/// consumed the same input.
#[test]
fn identical_seed_and_params_produce_byte_identical_csv_and_sha256() {
    let path_a = support::temp_csv("determinism-a");
    let path_b = support::temp_csv("determinism-b");

    let manifest_a = generate_to(&path_a, 500, 123, 0);
    let manifest_b = generate_to(&path_b, 500, 123, 0);

    let bytes_a = fs::read(&path_a).expect("read dataset a");
    let bytes_b = fs::read(&path_b).expect("read dataset b");
    assert_eq!(
        bytes_a, bytes_b,
        "two independent runs with identical parameters must be byte-identical"
    );
    assert_eq!(
        manifest_a["sha256"], manifest_b["sha256"],
        "SHA-256 must be identical for identical inputs"
    );
}

/// Changing the seed must change the generated data -- otherwise "seed"
/// would be decorative and distinct datasets could collide.
#[test]
fn different_seed_changes_generated_data() {
    let path_a = support::temp_csv("determinism-seed-a");
    let path_b = support::temp_csv("determinism-seed-b");

    let manifest_a = generate_to(&path_a, 500, 1, 0);
    let manifest_b = generate_to(&path_b, 500, 2, 0);

    assert_ne!(
        manifest_a["sha256"], manifest_b["sha256"],
        "different seeds must not produce the same content digest"
    );
    assert_ne!(
        fs::read(&path_a).expect("read dataset a"),
        fs::read(&path_b).expect("read dataset b")
    );
}

/// Known-answer regression guard for the rand 0.8 -> 0.10 / rand_chacha 0.3
/// -> 0.10 migration: this exact `(rows=50, seed=42, id_offset=0)` output
/// was generated and hand-verified byte-for-byte identical against the
/// pre-migration `rand` 0.8.8 + `rand_chacha` 0.3.1 build before being
/// hardcoded here (raw `ChaCha8Rng::seed_from_u64` output is unchanged
/// across the rand_chacha versions; the migration additionally replaced
/// `rand`'s own range-sampling algorithm -- confirmed via its CHANGELOG to
/// have changed under the hood -- with a version-pinned reimplementation in
/// `generator::uniform_range` specifically to keep this value stable). Any
/// future change to this generator's random-number usage that alters
/// output will fail this test instead of silently shipping.
#[test]
fn known_answer_output_for_fixed_seed_matches_pre_migration_baseline() {
    let path = support::temp_csv("determinism-known-answer");
    let manifest = generate_to(&path, 50, 42, 0);

    assert_eq!(manifest["file_size_bytes"], 3590);
    assert_eq!(
        manifest["sha256"],
        "7ed221365196779dd84b6d51387545e6f6f24a466b3680e9d88ff57265541803"
    );
}
