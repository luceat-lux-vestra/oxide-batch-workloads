# postgres-postgres

Independent external-consumer validation of published
[OxideBatch](https://github.com/luceat-lux-vestra/oxide-batch) **`0.6.0`**
(exact crates.io artifact with the `postgres` feature): a restartable
PostgreSQL-to-PostgreSQL transform exercised end to end through the released
`oxide_batch::JobLauncher` / `ChunkJob` path in both cursor and paging modes.

This workload is the completed campaign tracked by
[oxide-batch-workloads#63](../../issues/63). It covers clean execution,
cursor/paging parity, same-resource enlisted writes, typed rollback, real
process-crash recovery, source-identity isolation, and retained larger-dataset
resource observations.

## Architecture

```text
app_source.source_customer
        |
        | cursor: released postgres_cursor_reader
        | paging: released postgres_paging_reader
        v
   SourceRow
        |
        | deterministic CustomerProjector
        v
   ProjectedRow
        |
        | released postgres_batch_writer
        | PostgresBatchMode::MultiRowValues
        | enlisted in OxideBatch's same-resource transaction
        v
app_business.customer_projection
```

The workload uses three structurally separate PostgreSQL schemas:

| Schema | Owner | Contents |
|---|---|---|
| `oxide_batch` | OxideBatch | Framework durable metadata created through the released migrator/API. |
| `app_source` | workload | Deterministic source rows. |
| `app_business` | workload | Deterministic transformed destination rows. |

The production commands never directly mutate `oxide_batch.*`. The retained
evidence producer follows the same boundary: it requires a **fresh dedicated
database**, fails closed if `oxide_batch`, `app_source`, or `app_business`
already exists, and lets `postgres-postgres migrate` create framework metadata
through the released public API. It never truncates or deletes framework
metadata to make a rerun fit an already-used database.

The source ordering key is the strict unique `BIGINT NOT NULL PRIMARY KEY`
`customer_id`. The business projection is deterministic:

- `display_name` = upper-cased `full_name`;
- `loyalty_score` = `balance_cents / 100` using integer division;
- `is_premium` = `balance_cents >= 50_000`;
- `row_fingerprint` = the first 16 bytes of SHA-256 over the source business
  fields.

The destination primary key is `(import_name, source_digest, customer_id)`.
`reader_mode` is intentionally not part of the business key; cursor and paging
must produce the same business representation for identical source content.

## Source identity and stability

Before every run, `src/source_digest.rs` streams
`app_source.source_customer ORDER BY customer_id` with SQLx `fetch` and hashes
every business-significant source field. The digest is an identifying job
parameter alongside `import_name`.

The digest/read TOCTOU window is closed by
`src/source_digest.rs::lock_source_for_stable_read`: a dedicated transaction
holds `LOCK TABLE app_source.source_customer IN SHARE MODE` from before digest
computation until after the actual workload/verifier source read completes.
`tests/source_stability.rs` proves, for both reader modes, that the lock is held
and that a concurrent source write blocks until the guarded read finishes.

A source mutation therefore changes `source_digest`, resolves to a different
job identity, and cannot silently reuse a stale checkpoint. Crash/recovery
coverage for this invariant is in `tests/source_mutation_recovery.rs`.

## Reader modes

`run` requires `--reader cursor|paging`; there is no implicit default.

### Cursor

The workload calls the released `postgres_cursor_reader` directly.
`PostgresCursorFormat::with_fetch_size` bounds one `FETCH` result. The retained
run uses `fetch_size=500` over 200,000 rows: 400 data-bearing FETCH batches plus
the terminal empty FETCH used by the pinned implementation to discover EOF.

### Paging

The workload calls the released `postgres_paging_reader` directly.
`PostgresPagingFormat::with_page_size` bounds each keyset page. The reader uses
`WHERE (customer_id) > (last) ORDER BY customer_id LIMIT page_size`, never
`OFFSET`, and holds no server-side cursor resource between pages. The retained
run uses `page_size=750`, producing 267 page queries for 200,000 rows.

Mode-incompatible options and a zero page size fail closed; see
`tests/reader_config.rs`.

## Reader mode and job identity

`reader_mode` is a separate identifying `JobParameter`. Thus job identity is
effectively `(import_name, source_digest, reader_mode)`, while source identity
remains mode-independent.

Cursor and paging persist the same `KeysetPosition` shape under different
reader state contracts, stream namespaces, component revisions, and definition
revisions. The workload never cross-interprets cursor state as paging state or
vice versa. `tests/reader_mode_identity.rs` proves the two modes resolve to
distinct job instances over identical source content.

## Same-resource enlisted writer

`src/writer.rs` uses the released `postgres_batch_writer` directly with
`ChunkDeliveryMode::AtomicSameResource`. The writer receives the enlisted
`BusinessTransaction` supplied by OxideBatch; it does not open a private
connection or commit independently. Business rows and OxideBatch checkpoint /
component state therefore commit through the same transaction boundary.

For pinned OxideBatch 0.6.0, `PostgresBatchMode::multi_row_values()` configures
`max_parameters_per_statement=2000`. This workload writes 7 values per row, so
the released writer derives:

- `rows_per_statement = floor(2000 / 7) = 285`;
- maximum bound values in one full statement = `285 * 7 = 1,995`;
- with `chunk_size=1000`, at most `ceil(1000 / 285) = 4` INSERT statements per
  write chunk.

The retained records carry these values and the canonical verifier recomputes
the relationships. `7,000` is **not** a per-statement bind count.

## Independent verifier

`src/verify.rs` does not use the OxideBatch execution path and does not call the
production processor as its oracle. It independently derives expected rows,
streams source and destination in key order, merge-compares them, maintains
expected/actual running digests, and exits nonzero on any count, field, digest,
missing-row, or extra-row mismatch.

Both source and destination reads use streaming SQLx `fetch`. A durable
regression guard in `tests/no_whole_dataset_apis.rs` fails if
`src/source_digest.rs` or `src/verify.rs` reintroduces `.fetch_all(` on the
declared production streaming paths.

Mismatch diagnostics are also bounded: `VerifyReport` retains at most 100
examples while `total_mismatches` remains the exact total and drives the
fail-closed verdict. `tests/verifier_bounded_mismatches.rs` corrupts more than
the retained bound and verifies exact accounting plus truncation reporting.

## Rollback and real process-crash recovery

Campaign PR 3 proves the following independently for cursor and paging modes
using real child processes and the public OxideBatch API:

1. A typed failure after business writes but before commit rolls the whole
   target chunk back while preserving earlier committed chunks.
2. A real `SIGKILL` after business writes but before commit leaves neither the
   target chunk's business rows nor its checkpoint durable.
3. A real `SIGKILL` immediately after a successful atomic commit leaves both
   business rows and checkpoint durable together.
4. Recovery is performed through the public recovery/operator API and
   continuation executes in a genuinely new OS process.
5. Recovered final business output is representation-equivalent to the clean
   run with no missing, extra, or duplicate rows.
6. Mutating source content after a crash produces a new identity and cannot
   reuse the crashed instance's stale checkpoint.
7. Negative recovery cases fail closed rather than guessing an execution.

Relevant tests: `tests/rollback.rs`, `tests/crash_before_commit.rs`,
`tests/crash_after_commit.rs`, `tests/source_mutation_recovery.rs`, and
`tests/recover_negative.rs`.

## Retained larger-dataset evidence

`validation/` is governed by the repository-wide
[`.github/EVIDENCE_CONTRACT.md`](../.github/EVIDENCE_CONTRACT.md):

- `evidence-manifest.json` binds the producer revision, semantic closure,
  exact published OxideBatch subject, retained artifacts, canonical verifier,
  environment observations, and deterministic retention bounds;
- `generate-evidence.sh` is the deterministic producer and is part of the
  semantic closure;
- `cursor-run.json` and `paging-run.json` are the committed retained records;
- `verify-retained-evidence.py` is the canonical verifier and returns the
  `violations-v1` model;
- `test_verify_retained_evidence.py` contains positive/negative controls.

The canonical verifier derives row-count and digest relationships from
retained primitive values. Producer-authored booleans are not authoritative
pass/fail evidence.

### Retained configuration

The ordinary CI smoke dataset is 2,000 rows. The retained evidence uses a
100x larger deterministic dataset:

| Parameter | Value |
|---|---|
| rows | 200,000 |
| seed | 20260904 |
| id_offset | 0 |
| chunk_size | 1,000 (200 chunks) |
| cursor fetch_size | 500 |
| paging page_size | 750 |
| writer | `postgres_batch_writer`, `MultiRowValues`, 7 columns/row |
| PostgreSQL | `18.6 (Debian 18.6-1.pgdg13+2)` |
| rustc | `1.98.1` |
| cargo | `1.98.1` |
| build profile | release |

Both retained runs share source digest
`55f651a1eff2c4cd7e1da1bb67fb1e04bcc8672e713defebcd724cdededaf79c`,
complete all 200 chunks, read/write 200,000 rows, and independently verify
with zero mismatches. The expected and actual destination digests are equal:
`de250fdb6cf9dc2bf919d554c66a076d857a318ffa494c89c0f6c4528f641d89`.

### Peak RSS observation

`generate-evidence.sh` measures each workload process externally by polling
Linux `/proc/<pid>/status` `VmHWM` every 20ms for the process lifetime. These
are observational measurements, not merge thresholds:

| Process | Peak RSS | Runtime |
|---|---:|---:|
| cursor `run` | 10,896 KiB | 2.524 s |
| cursor `verify` | 7,720 KiB | 0.518 s |
| paging `run` | 10,892 KiB | 2.353 s |
| paging `verify` | 7,752 KiB | 0.563 s |

The producer ran on a GitHub Actions `ubuntu-24.04` hosted runner
(`Linux 6.17.0-1022-azure x86_64`) against the workload's PostgreSQL 18
Docker service. The one-shot execution harness created a **fresh dedicated
database inside that service** before invoking the exact producer script.
The harness was execution-only and removed before final review.

`run` peak RSS includes its source-digest computation because the digest is
computed in the same process before the reader launches. `verify` likewise
includes its independent source-digest recomputation. The measurements do not
isolate source-digest RSS from the rest of either process.

These single-host observations are consistent with the structural bounds in
the reader, writer, source-digest path, and verifier, but they are not an
asymptotic proof and are not suitable as cross-host performance comparisons.
There is no hosted-runner RSS or throughput hard threshold.

## Evidence reproduction

Use a fresh dedicated PostgreSQL database. The producer intentionally refuses
to reuse a database that already contains any of the campaign schemas.

```bash
DATABASE_URL=postgresql://oxide_batch_workload:oxide_batch_workload@localhost:5434/fresh_postgres_postgres_evidence \
  ./validation/generate-evidence.sh
```

To verify retained evidence:

```bash
python3 validation/verify-retained-evidence.py --manifest validation/evidence-manifest.json
python3 ../.github/scripts/validate-evidence.py
python3 validation/test_verify_retained_evidence.py
```

The manifest's current producer base revision is
`4df4a8426689a2a91fcc68a1c6132577d30cf5ec`, with semantic-closure digest
`1174985a8a2bc7556081551b936af2f97c67a361e97fecbdc20cf29e7da376e7`.
The producer execution is recorded as GitHub Actions run `33916280736` with
`recorded-metadata` trust. Manifest v1 does not implement an external
attestation verifier, so the evidence does **not** claim
`trusted-producer-bound` authenticity.

## Evidence limitations

- One dataset size, one hosted runner, one retained run per reader mode; no
  repeated-trial distribution, soak, or longevity claim.
- Peak RSS is sampled every 20ms rather than traced continuously; an extremely
  short-lived peak between polls could theoretically be missed.
- CPU count, memory limit, and disk-throughput observations are not retained,
  so no cross-host resource comparison is claimed.
- The GitHub Actions run ID gives durable execution metadata but is not a
  cryptographic attestation tying every retained byte to that run.
- The campaign proves the specified commit-boundary crash cases, not every
  conceivable crash point such as mid-read or mid-page-fetch.
- This is not a general exactly-once, high-availability, or production-
  readiness claim.

## Known OxideBatch 0.6.0 limitations

- [oxide-batch#218](https://github.com/luceat-lux-vestra/oxide-batch/issues/218):
  `BusinessValue` has no temporal/TIMESTAMPTZ variant. This workload's schema
  deliberately avoids temporal columns rather than adding an unreleased
  workaround.
- [oxide-batch#220](https://github.com/luceat-lux-vestra/oxide-batch/issues/220):
  DB writer/transaction errors are value-redacted at the public consumer
  boundary, so SQLSTATE/constraint/driver detail is not recoverable there.
  Campaign #63 did not require that detail.

## Campaign acceptance matrix

| Obligation | Status | Evidence |
|---|---|---|
| Exact published `oxide-batch = "=0.6.0"` | PROVEN | Cargo provenance validator + manifest validation subject |
| PostgreSQL 18 external workload | PROVEN | CI integration + retained PostgreSQL 18.6 run |
| Cursor bounded streamed reader | PROVEN | released cursor reader, `reader_bounds`, retained cursor record |
| Paging bounded keyset reader, no OFFSET | PROVEN | released paging reader, paging tests, retained paging record |
| Cursor/paging business parity | PROVEN | `tests/reader_parity.rs` + shared retained source identity |
| Reader mode is distinct job identity | PROVEN | `tests/reader_mode_identity.rs` |
| Enlisted same-resource writer | PROVEN | released writer + clean/paging/crash tests |
| Writer statement/sub-batch boundedness | PROVEN | pinned 0.6.0 arithmetic retained and canonically re-derived |
| Source digest streams without whole-dataset materialization | PROVEN | `src/source_digest.rs` + `no_whole_dataset_apis` |
| Independent verifier | PROVEN | `src/verify.rs` + negative controls |
| Verifier diagnostic boundedness | PROVEN | `MAX_RETAINED_MISMATCHES` + adversarial corruption test |
| Source stability / TOCTOU guard | PROVEN | `tests/source_stability.rs` |
| Typed pre-commit rollback | PROVEN | `tests/rollback.rs` |
| Hard crash before commit | PROVEN | `tests/crash_before_commit.rs` |
| Hard crash immediately after commit | PROVEN | `tests/crash_after_commit.rs` |
| Genuine new-process recovery | PROVEN | crash/recovery tests |
| Source-mutation stale-checkpoint isolation | PROVEN | `tests/source_mutation_recovery.rs` |
| Canonical retained evidence contract | PROVEN | manifest v1 + repository auto-discovery validator |
| Materially larger retained dataset | PROVEN | 200,000 rows vs CI's 2,000 rows |
| Cursor/paging resource observations | PROVEN | retained JSON + process-level VmHWM measurements |
| Producer never directly resets framework metadata | PROVEN | fresh-database fail-closed producer + GitHub Actions reproduction |
| Benchmark / raw sqlx / Spring Batch comparison | N/A | separate later campaign, deliberately out of #63 |
| Scheduler/control-plane interoperability | N/A | separate later work |
| Temporal `BusinessValue` support | N/A | upstream #218 |

## Commands

```text
postgres-postgres migrate --database-url <url>
postgres-postgres seed --database-url <url> --rows <n> --seed <n> [--id-offset <n>]
postgres-postgres run --database-url <url> --import-name <name> --reader cursor|paging [--chunk-size <n>] [--fetch-size <n> | --page-size <n>]
postgres-postgres recover --database-url <url> --import-name <name> --reader cursor|paging
postgres-postgres verify --database-url <url> --import-name <name>
postgres-postgres reset --database-url <url>
```

`reset` truncates only workload-owned `app_source` / `app_business` tables; it
never touches `oxide_batch` framework metadata.

## Local development

```bash
docker compose up -d --wait
cargo run -- migrate
cargo run -- seed --rows 2000 --seed 42
cargo run -- run --import-name demo --reader cursor --chunk-size 200 --fetch-size 128
cargo run -- verify --import-name demo
cargo run -- run --import-name demo_paging --reader paging --chunk-size 200 --page-size 128
cargo run -- verify --import-name demo_paging
```

`ci/validate ci` is the workload's authoritative integration contract for
formatting, clippy, build, production-path guards, canonical retained-evidence
checks, real PostgreSQL tests, and golden-path smoke execution. Repository
GitHub Actions remains the authoritative merge gate.
