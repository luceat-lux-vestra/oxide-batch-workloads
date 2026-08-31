//! Deterministic synthetic CSV dataset generator.
//!
//! Same `(rows, seed, variant)` always produces byte-identical output: no
//! wall-clock, no OS entropy, no HashMap iteration order. This is what lets a
//! clean run and a restart run be proven to have consumed exactly the same
//! input (see `Sha256Digest` below and the CLI `generate` command).

use std::io::Write;
use std::path::Path;

use chrono::{DateTime, Duration, TimeZone, Utc};
use rand::Rng;
use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use serde::Serialize;
use sha2::{Digest, Sha256};

/// Which edge-case shape a generated row takes. Plain data is the default;
/// the others exist to give the pipeline something real to reject.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RowVariant {
    Normal,
    DuplicateKey,
    MalformedFieldCount,
    MalformedAmount,
    QuotedField,
    EscapedQuoteField,
}

/// Dataset size profile. See README for the recommended row counts per
/// profile (tiny/normal/stress).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Profile {
    Tiny,
    Normal,
    Stress,
}

impl Profile {
    #[must_use]
    pub const fn default_rows(self) -> u64 {
        match self {
            Self::Tiny => 1_000,
            Self::Normal => 100_000,
            Self::Stress => 1_000_000,
        }
    }

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Tiny => "tiny",
            Self::Normal => "normal",
            Self::Stress => "stress",
        }
    }
}

impl std::str::FromStr for Profile {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "tiny" => Ok(Self::Tiny),
            "normal" => Ok(Self::Normal),
            "stress" => Ok(Self::Stress),
            other => Err(format!(
                "unknown profile '{other}' (expected tiny|normal|stress)"
            )),
        }
    }
}

/// Which deterministic 1-based row indices carry each edge-case variant,
/// for a dataset of `rows` total rows. Kept small and fixed so the same
/// `rows` value always edge-cases the same positions.
fn edge_case_plan(rows: u64) -> Vec<(u64, RowVariant)> {
    let mut plan = Vec::new();
    if rows >= 5 {
        plan.push((rows / 5, RowVariant::QuotedField));
    }
    if rows >= 4 {
        plan.push((rows / 4, RowVariant::EscapedQuoteField));
    }
    plan
}

#[derive(Serialize)]
pub struct GenerateManifest {
    pub rows: u64,
    pub seed: u64,
    pub file_size_bytes: u64,
    pub sha256: String,
    pub generated_at: DateTime<Utc>,
}

fn quote_csv_field(field: &str) -> String {
    if field.contains(',') || field.contains('"') || field.contains('\n') {
        format!("\"{}\"", field.replace('"', "\"\""))
    } else {
        field.to_owned()
    }
}

fn base_row(
    rng: &mut ChaCha8Rng,
    row_index: u64,
    id_offset: u64,
) -> (u64, String, String, i64, DateTime<Utc>) {
    let customer_id = id_offset + row_index;
    let first_names = [
        "Alice", "Bob", "Carol", "Dave", "Erin", "Frank", "Grace", "Heidi", "Ivan", "Judy",
    ];
    let last_names = [
        "Smith", "Johnson", "Lee", "Brown", "Garcia", "Miller", "Davis", "Wilson", "Moore",
        "Taylor",
    ];
    let first = first_names[(row_index as usize) % first_names.len()];
    let last = last_names[rng.gen_range(0..last_names.len())];
    let name = format!("{first} {last}");
    let email = format!("customer{customer_id}@example.test");
    let amount = rng.gen_range(100..1_000_000);
    let base = Utc
        .with_ymd_and_hms(2026, 1, 1, 0, 0, 0)
        .single()
        .expect("fixed calendar date is always valid");
    let created_at = base + Duration::seconds(row_index as i64);
    (customer_id, name, email, amount, created_at)
}

fn write_row(
    out: &mut impl Write,
    variant: RowVariant,
    customer_id: u64,
    name: &str,
    email: &str,
    amount: i64,
    created_at: DateTime<Utc>,
) -> std::io::Result<()> {
    match variant {
        RowVariant::Normal | RowVariant::DuplicateKey => {
            writeln!(
                out,
                "{customer_id},{},{email},{amount},{}",
                quote_csv_field(name),
                created_at.to_rfc3339(),
            )
        }
        RowVariant::MalformedFieldCount => {
            // Missing the trailing created_at field entirely.
            writeln!(
                out,
                "{customer_id},{},{email},{amount}",
                quote_csv_field(name)
            )
        }
        RowVariant::MalformedAmount => {
            writeln!(
                out,
                "{customer_id},{},{email},NOT_A_NUMBER,{}",
                quote_csv_field(name),
                created_at.to_rfc3339(),
            )
        }
        RowVariant::QuotedField => {
            let quoted_name = format!("Smith, {name}");
            writeln!(
                out,
                "{customer_id},\"{quoted_name}\",{email},{amount},{}",
                created_at.to_rfc3339(),
            )
        }
        RowVariant::EscapedQuoteField => {
            let quoted_name = format!("{name} \"\"the great\"\"");
            writeln!(
                out,
                "{customer_id},\"{quoted_name}\",{email},{amount},{}",
                created_at.to_rfc3339(),
            )
        }
    }
}

/// Generates `rows` deterministic CSV rows (no header) at `path`, seeded by
/// `seed`, and returns a manifest recording row count/seed/size/SHA-256 --
/// the evidence that a clean run and a restart run consumed identical input.
///
/// `duplicate_at` re-emits the row immediately preceding it with the same
/// `customer_id` (a real duplicate business key), if `Some`.
///
/// # Errors
///
/// Returns an I/O error if `path` cannot be created or written.
pub fn generate(
    path: &Path,
    rows: u64,
    seed: u64,
    duplicate_at: Option<u64>,
    malformed_at: Option<u64>,
    bad_amount_at: Option<u64>,
    id_offset: u64,
) -> std::io::Result<GenerateManifest> {
    let mut rng = ChaCha8Rng::seed_from_u64(seed);
    let edge_cases = edge_case_plan(rows);
    let file = std::fs::File::create(path)?;
    let mut writer = std::io::BufWriter::new(file);
    let mut last_row: Option<(u64, String, String, i64, DateTime<Utc>)> = None;

    for row_index in 1..=rows {
        let row = base_row(&mut rng, row_index, id_offset);
        let variant = edge_cases
            .iter()
            .find(|(at, _)| *at == row_index)
            .map(|(_, variant)| *variant)
            .unwrap_or(RowVariant::Normal);
        let variant = if malformed_at == Some(row_index) {
            RowVariant::MalformedFieldCount
        } else if bad_amount_at == Some(row_index) {
            RowVariant::MalformedAmount
        } else {
            variant
        };
        write_row(&mut writer, variant, row.0, &row.1, &row.2, row.3, row.4)?;

        if duplicate_at == Some(row_index) {
            if let Some((id, name, email, amount, created_at)) = &last_row {
                write_row(
                    &mut writer,
                    RowVariant::DuplicateKey,
                    *id,
                    name,
                    email,
                    *amount,
                    *created_at,
                )?;
            }
        }
        last_row = Some(row);
    }
    writer.flush()?;
    drop(writer);

    let bytes = std::fs::read(path)?;
    let file_size_bytes = bytes.len() as u64;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    let sha256 = format!("{:x}", hasher.finalize());

    Ok(GenerateManifest {
        rows,
        seed,
        file_size_bytes,
        sha256,
        generated_at: Utc::now(),
    })
}

/// Computes the SHA-256 of a file already on disk, for job-identity and
/// input-mutation-guard purposes (see `job::input_identity`).
///
/// # Errors
///
/// Returns an I/O error if `path` cannot be read.
pub fn sha256_of_file(path: &Path) -> std::io::Result<String> {
    let bytes = std::fs::read(path)?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok(format!("{:x}", hasher.finalize()))
}
