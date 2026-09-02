//! Bounded, non-product CI-orchestration fixture.
//!
//! This is deliberately not an OxideBatch workload: no database, no
//! services, no framework dependency. Its only job is to have a validation
//! shape (deterministic word-histogram over a text file, verified against a
//! checked-in golden file) that is materially different from
//! `csv-postgres`'s PostgreSQL/migration/restart shape, so that
//! `ci/validate` proves the central CI workflow's fan-out is contract-driven
//! rather than hardcoded to one workload.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::process::ExitCode;

fn histogram(text: &str) -> BTreeMap<String, u64> {
    let mut counts: BTreeMap<String, u64> = BTreeMap::new();
    for word in text.split(|c: char| !c.is_alphanumeric()) {
        if word.is_empty() {
            continue;
        }
        *counts.entry(word.to_lowercase()).or_insert(0) += 1;
    }
    counts
}

fn run() -> Result<(), String> {
    let mut args = env::args().skip(1);
    let command = args
        .next()
        .ok_or("usage: fixture-heterogeneous histogram <input-path>")?;
    if command != "histogram" {
        return Err(format!("unknown command: {command}"));
    }
    let input_path = args
        .next()
        .ok_or("usage: fixture-heterogeneous histogram <input-path>")?;
    let text =
        fs::read_to_string(&input_path).map_err(|e| format!("cannot read {input_path}: {e}"))?;
    let counts = histogram(&text);
    let rendered = serde_json::to_string_pretty(&counts)
        .map_err(|e| format!("cannot render histogram: {e}"))?;
    println!("{rendered}");
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_words_case_insensitively_and_deterministically() {
        let counts = histogram("Alpha beta alpha\nBETA gamma");
        assert_eq!(counts.get("alpha"), Some(&2));
        assert_eq!(counts.get("beta"), Some(&2));
        assert_eq!(counts.get("gamma"), Some(&1));
        assert_eq!(counts.len(), 3);
    }

    #[test]
    fn ignores_punctuation_and_whitespace_only_input() {
        let counts = histogram("... --- ,,, \n\t");
        assert!(counts.is_empty());
    }
}
