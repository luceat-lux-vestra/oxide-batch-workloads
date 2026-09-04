//! Campaign #63 PR 4 / proof obligation P8: a durable, automated guard that
//! fails if the two production paths this campaign establishes as
//! streaming/bounded -- `src/source_digest.rs` and `src/verify.rs` -- ever
//! regress to an obvious whole-dataset API such as `sqlx`'s `.fetch_all(`.
//!
//! This is a source-text guard, not a runtime behavioral test: it reads the
//! two production files' own text at test time (not `include_str!`, which
//! would bind the check to whatever revision this test binary was compiled
//! from) and asserts the banned substring never appears in them. Scope is
//! deliberately narrow -- only these two production files, not the whole
//! crate -- so test/support code (which may legitimately use `fetch_all`,
//! e.g. `tests/support/mod.rs::full_projection_rows`) is never affected, and
//! this guard cannot accidentally fail on a legitimate, bounded use of
//! `fetch_all` elsewhere in the crate (there is none today, but this guard
//! makes no claim about files outside its declared scope).
//!
//! Ordinary compiled-in Rust code cannot regress `src/source_digest.rs` or
//! `src/verify.rs` to `fetch_all` without this test catching it on the very
//! next `cargo test` run -- a genuine regression could not land without
//! `ci/validate ci` failing first.

use std::path::Path;

/// Banned substring: the actual method-call syntax (`.fetch_all(`), not the
/// bare identifier -- both guarded files' own module documentation
/// legitimately *talks about* `fetch_all` in prose (contrasting it with the
/// streaming `fetch` they actually use), and a bare-identifier match would
/// false-positive on that prose rather than on a real call.
const BANNED: &str = ".fetch_all(";

const GUARDED_PRODUCTION_FILES: &[&str] = &["src/source_digest.rs", "src/verify.rs"];

#[test]
fn production_streaming_paths_never_use_fetch_all() {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    for relative_path in GUARDED_PRODUCTION_FILES {
        let path = manifest_dir.join(relative_path);
        let contents = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
        assert!(
            !contents.contains(BANNED),
            "{} must never call `{BANNED}` on its production read path -- this file is a \
             declared bounded-memory streaming implementation (see its own module \
             documentation); if a real requirement now needs whole-dataset materialization, \
             that is a deliberate, reviewed design change, not something this guard should be \
             silently widened to permit",
            path.display()
        );
    }
}
