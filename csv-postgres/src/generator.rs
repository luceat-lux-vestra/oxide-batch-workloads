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

/// Uniform integer sampling in `[low, high_exclusive)`, built only on
/// `next_u64` -- deliberately *not* `rand`'s own `gen_range`/`random_range`.
///
/// `rand_core`'s `SeedableRng`/`Rng` contract guarantees a fixed generator's
/// raw output stream for a given seed is stable across crate versions, but
/// `rand`'s higher-level range-sampling algorithm carries no such guarantee:
/// rand 0.9 switched single-sample integer distributions to Canon's/Lemire's
/// method specifically because it "breaks value stability" (rand's own
/// CHANGELOG), and separately special-cased `usize`/`isize` sampling
/// (`UniformUsize`) for 32-/64-bit portability. Empirically, the generator's
/// `usize` range changed under rand 0.10 while its current `i64` range happened
/// to match; relying on either behavior would make future output stability
/// depend on rand's internal distribution implementation.
///
/// This function instead pins the exact widening-multiply-with-rejection
/// algorithm `rand` 0.8's `UniformInt<u64>` used, so `generate`'s
/// byte-identical-output promise (see module docs) does not silently drift
/// the next time `rand` changes its distribution internals. Verified
/// bit-for-bit against `rand` 0.8.8 + `rand_chacha` 0.3.1 across 2,000 seeds.
fn uniform_range(rng: &mut ChaCha8Rng, low: u64, high_exclusive: u64) -> u64 {
    let range = high_exclusive - low;
    let zone = (range << range.leading_zeros()).wrapping_sub(1);
    loop {
        let v: u64 = rng.next_u64();
        let full = u128::from(v) * u128::from(range);
        let hi = (full >> 64) as u64;
        let lo = full as u64;
        if lo <= zone {
            return low + hi;
        }
    }
}

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
    let last = last_names[uniform_range(rng, 0, last_names.len() as u64) as usize];
    let name = format!("{first} {last}");
    let email = format!("customer{customer_id}@example.test");
    let amount = uniform_range(rng, 100, 1_000_000) as i64;
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

    let file_size_bytes = std::fs::metadata(path)?.len();
    let sha256 = sha256_of_file(path)?;

    Ok(GenerateManifest {
        rows,
        seed,
        file_size_bytes,
        sha256,
        generated_at: Utc::now(),
    })
}

/// The read buffer size for `sha256_of_file`: fixed regardless of input
/// size, so hashing a 1 GB file costs the same working memory as hashing a
/// 1 KB one.
const HASH_BUFFER_BYTES: usize = 64 * 1024;

/// Computes the SHA-256 of a file already on disk, for job-identity and
/// input-mutation-guard purposes (see `job::run`), by streaming it through
/// a fixed-size buffer rather than reading it into memory whole -- the
/// import pipeline's own streaming-memory claim would otherwise be
/// contradicted by this step alone, which runs before the streaming reader
/// ever opens the file.
///
/// # Errors
///
/// Returns an I/O error if `path` cannot be read.
pub fn sha256_of_file(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    let mut file = std::io::BufReader::new(std::fs::File::open(path)?);
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; HASH_BUFFER_BYTES];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(crate::hex::hex_digest(&hasher.finalize()))
}
