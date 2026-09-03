//! Deterministic synthetic source-row generator.
//!
//! Same `(rows, seed, id_offset)` always produces byte-identical
//! `app_source.source_customer` content: no wall-clock, no OS entropy, no
//! `HashMap` iteration order. This is what makes the source content digest
//! (`source_digest::compute`) reproducible run to run, and lets a changed
//! seed be proven to resolve to a changed source identity (see
//! `tests/source_identity.rs`).
//!
//! Rows are streamed into the database in bounded batches (`BATCH_ROWS`),
//! never buffered as one in-memory `Vec` covering the whole dataset.

use rand::Rng;
use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use sqlx::AssertSqlSafe;

/// Deliberately built only on `next_u64`, not `rand`'s higher-level
/// range-sampling API: `rand_core`'s `SeedableRng`/`Rng` contract
/// guarantees a fixed generator's raw output stream is stable for a given
/// seed, but the higher-level distribution algorithm carries no such
/// guarantee across `rand` versions. Pinning this widening-multiply
/// algorithm by hand keeps `seed(rows, seed)`'s determinism from silently
/// drifting the next time `rand` changes its internals.
fn uniform_range(rng: &mut ChaCha8Rng, low: u64, high_exclusive: u64) -> u64 {
    let range = high_exclusive - low;
    let zone = (range << range.leading_zeros()).wrapping_sub(1);
    loop {
        let v = rng.next_u64();
        let full = u128::from(v) * u128::from(range);
        let hi = (full >> 64) as u64;
        let lo = full as u64;
        if lo <= zone {
            return low + hi;
        }
    }
}

/// How many generated rows are bound into one `INSERT` statement: bounds
/// this generator's memory and bind-parameter count to `O(BATCH_ROWS)`
/// regardless of total row count.
const BATCH_ROWS: usize = 500;
const COLUMNS_PER_ROW: usize = 4;

const FIRST_NAMES: [&str; 10] = [
    "Alice", "Bob", "Carol", "Dave", "Erin", "Frank", "Grace", "Heidi", "Ivan", "Judy",
];
const LAST_NAMES: [&str; 10] = [
    "Smith", "Johnson", "Lee", "Brown", "Garcia", "Miller", "Davis", "Wilson", "Moore", "Taylor",
];

struct GeneratedRow {
    customer_id: i64,
    full_name: String,
    is_active: bool,
    balance_cents: i64,
}

fn generate_row(rng: &mut ChaCha8Rng, row_index: u64, id_offset: u64) -> GeneratedRow {
    let customer_id = (id_offset + row_index) as i64;
    let first = FIRST_NAMES[(row_index as usize) % FIRST_NAMES.len()];
    let last = LAST_NAMES[uniform_range(rng, 0, LAST_NAMES.len() as u64) as usize];
    let is_active = uniform_range(rng, 0, 10) != 0; // ~90% active
    let balance_cents = uniform_range(rng, 0, 1_000_000) as i64; // up to $10,000.00
    GeneratedRow {
        customer_id,
        full_name: format!("{first} {last}"),
        is_active,
        balance_cents,
    }
}

fn insert_sql(row_count: usize) -> String {
    let mut param = 1usize;
    let rows = (0..row_count)
        .map(|_| {
            let (c1, c2, c3, c4) = (param, param + 1, param + 2, param + 3);
            param += COLUMNS_PER_ROW;
            format!("(${c1}, ${c2}, ${c3}, ${c4})")
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "INSERT INTO app_source.source_customer (customer_id, full_name, is_active, balance_cents) VALUES {rows}"
    )
}

/// Generates `rows` deterministic rows into `app_source.source_customer`,
/// seeded by `seed`, offset by `id_offset` (so independent test runs
/// sharing one database never collide on `customer_id`).
///
/// # Errors
///
/// Returns an error if a batch insert fails.
pub async fn seed(pool: &sqlx::PgPool, rows: u64, seed: u64, id_offset: u64) -> anyhow::Result<()> {
    let mut rng = ChaCha8Rng::seed_from_u64(seed);
    let mut row_index = 1u64;
    while row_index <= rows {
        let batch_len = BATCH_ROWS.min((rows - row_index + 1) as usize);
        let batch: Vec<GeneratedRow> = (0..batch_len)
            .map(|offset| generate_row(&mut rng, row_index + offset as u64, id_offset))
            .collect();
        let sql = insert_sql(batch.len());
        // `sql` is fixed, hand-built parameterized SQL text (only the
        // placeholder count varies with batch size); every actual data
        // value is still separately bound below, never interpolated.
        let mut query = sqlx::query(AssertSqlSafe(sql));
        for row in &batch {
            query = query
                .bind(row.customer_id)
                .bind(&row.full_name)
                .bind(row.is_active)
                .bind(row.balance_cents);
        }
        query.execute(pool).await?;
        row_index += batch_len as u64;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_sql_places_one_placeholder_group_per_row() {
        assert_eq!(
            insert_sql(1),
            "INSERT INTO app_source.source_customer (customer_id, full_name, is_active, balance_cents) VALUES ($1, $2, $3, $4)"
        );
        assert_eq!(
            insert_sql(2),
            "INSERT INTO app_source.source_customer (customer_id, full_name, is_active, balance_cents) VALUES ($1, $2, $3, $4), ($5, $6, $7, $8)"
        );
    }

    #[test]
    fn generate_row_is_deterministic_for_a_fixed_seed() {
        let mut rng_a = ChaCha8Rng::seed_from_u64(42);
        let mut rng_b = ChaCha8Rng::seed_from_u64(42);
        let a = generate_row(&mut rng_a, 1, 0);
        let b = generate_row(&mut rng_b, 1, 0);
        assert_eq!(a.customer_id, b.customer_id);
        assert_eq!(a.full_name, b.full_name);
        assert_eq!(a.is_active, b.is_active);
        assert_eq!(a.balance_cents, b.balance_cents);
    }

    #[test]
    fn different_seeds_diverge() {
        let mut rng_a = ChaCha8Rng::seed_from_u64(42);
        let mut rng_b = ChaCha8Rng::seed_from_u64(4242);
        let a = generate_row(&mut rng_a, 1, 0);
        let b = generate_row(&mut rng_b, 1, 0);
        assert!(a.full_name != b.full_name || a.balance_cents != b.balance_cents);
    }
}
