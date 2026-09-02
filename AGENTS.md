# Agent Instructions

## Mission

This repository is an independent validation suite for **published OxideBatch releases**. Its workloads answer, with reproducible evidence, whether a real external consumer can use a released artifact correctly and what transaction, restart, recovery, resource, diagnostic, and performance properties are actually observable.

This repository is deliberately separate from `luceat-lux-vestra/oxide-batch`. A workload must consume the public release exactly as an external application would. Framework defects discovered here become issues and separately reviewed changes in the framework repository; never make a workload pass by reaching into, patching, or depending on a local framework checkout.

## Non-negotiable rules

- Depend on an exact published OxideBatch version, for example `oxide-batch = "=0.6.0"`.
- The committed lockfile must resolve OxideBatch from the public registry. No path dependency, git dependency, workspace override, `[patch]`, vendored modified framework, or unpublished framework artifact may satisfy validation.
- Never edit framework source from this repository to make a workload pass.
- Preserve correctness and recovery semantics before throughput, ergonomics, or benchmark numbers.
- Treat claims such as `exactly-once`, `restartable`, `bounded memory`, `production-ready`, `faster`, and `compatible` as evidence claims, not adjectives.
- Do not hide a framework limitation behind a large consumer-side abstraction. Record the limitation, create/link a minimal framework issue, and keep any workload workaround explicit.
- Do not silently weaken a test, failure injection point, verifier, dataset, or assertion to obtain green CI.

## Repository structure and ownership

Each top-level workload is an independent consumer project. It owns its own dependency version, lockfile, fixtures/generator, database schema, tests, documentation, and validation evidence. A workload must not depend on another workload's implementation details.

`csv-postgres/` validates the published OxideBatch 0.6.0 CSV-to-PostgreSQL restartable chunk workload. Future workloads must be added because they test a materially different real-world contract, not merely to increase example count.

CI is registry- and contract-driven, not workload-name-branched: the central workflow (`.github/workflows/ci.yml`) only knows how to validate/discover `workloads.json`, fan out over its entries, invoke each entry's own `<entry>/ci/validate <ci|msrv>` entrypoint, and compute an aggregate verdict with `.github/scripts/aggregate_verdict.py` that is policy-aware (a workload's MSRV outcome must match its own registered `msrv.declared` policy, not merely "the shard succeeded"). See `.github/WORKLOAD_CONTRACT.md` for the exact contract. `workloads.json` structurally separates real `workloads` (always subject to #29's exact-published-provenance enforcement — `validate-oxidebatch-provenance.py` never reads any other key) from `fixtures`. `fixture-heterogeneous/` is registered under `fixtures`, not `workloads`: it is not a validation workload, makes no OxideBatch claim, and exists solely to prove the fan-out is contract-driven in real CI. A real new workload must always be added under `workloads` and follow the process in "Adding a workload" in the top-level `README.md` — there is no per-entry flag that exempts a `workloads` entry from #29.

Framework-owned durable metadata and workload-owned business data are separate concerns. Production workload commands must use public OxideBatch APIs for framework metadata. Direct manipulation of framework metadata is allowed only in clearly identified disposable test/evidence teardown when no public cleanup API exists; document every such exception.

## Required workflow before implementation

For non-trivial changes:

1. Read this file and the repository `README.md`.
2. Read the affected workload's README and its existing validation evidence.
3. Inspect the exact published OxideBatch API/version that the workload consumes; do not reason from framework `main` when validating an older release.
4. Classify impact on correctness, transactions, checkpoints, restart/recovery, idempotency, input identity, resource bounds, diagnostics, evidence reproducibility, and public claims.
5. If behavior is unclear, design a deterministic experiment or failing test before changing the implementation.
6. If the result exposes a framework limitation, search for an existing framework issue before creating a new one and link the finding from workload documentation.

Do not add speculative architecture, wrappers, extension layers, or generalized workload infrastructure without demonstrated reuse and a concrete boundary.

## Correctness and restart bar

A restart test is not a same-process retry. Where restart behavior is claimed, exercise a new process after a real process termination at meaningful lifecycle or commit boundaries.

For stateful workloads, distinguish at least:

- work attempted before a transaction commits;
- business commit/checkpoint atomicity or ambiguity;
- persisted checkpoint position;
- rows/chunks reprocessed after recovery;
- duplicate attempts versus duplicate final business state;
- framework guarantees versus application idempotency.

Never infer `exactly-once` from a duplicate-free final table. A unique constraint or idempotent writer can produce an effectively-once final state over an at-least-once processing model. State the observed contract precisely.

Input used for restart must have an explicit identity. A changed file at the same path must not silently resume as though it were the original input.

## Verification and evidence

Verification must be independent enough to catch defects in the code path under test. Never compare a value to itself, reuse the same flawed transformation on both sides without justification, or validate only row counts when content correctness is claimed.

When comparing source and destination data:

- parse the real source format;
- compare all business-significant fields;
- make ordering assumptions explicit and validate them, or use a bounded-memory order-independent method with documented collision/duplicate semantics;
- include a negative control that proves deliberate corruption is detected.

Committed evidence must identify enough context to reproduce and interpret the result, including as applicable:

- workload commit SHA;
- exact OxideBatch version and registry source;
- lockfile state;
- dataset row count, deterministic seed, byte size, and SHA-256;
- chunk size and failure injection point;
- Rust/toolchain, OS/runner, database version;
- clean-run and recovered-run final-state digests;
- resume point, reprocessing, duplicates, runtime, and resource observations.

Evidence files are not decorative snapshots. If implementation or environment changes invalidate their meaning, regenerate them or explicitly document why the existing evidence remains valid. Never hand-edit generated evidence to manufacture agreement.

## Failure injection

Failure injection must be deterministic and named by a stable semantic point, such as a row, chunk, before-commit, after-business-commit, or checkpoint boundary. Random-only failure is insufficient for merge-gate evidence.

At least one restart scenario for a restartable workload must use a hard process failure such as abort/kill and a newly launched process. Graceful error handling and hard-crash recovery are different evidence classes.

A failure-path test must assert durable state and business state, not merely a nonzero exit code or log message.

## Resource and performance discipline

Workloads representing streaming or large-data behavior must keep memory, connections, file handles, transactions, queues, and buffers bounded with respect to total input size. Avoid whole-file reads and unbounded `fetch_all`/collection on production or verification paths when the workload claims streaming behavior.

Performance results are valid only after correctness and recovery gates pass. Benchmarks must use like-for-like semantics and record the environment. Prefer this comparison order:

1. raw language/database-driver baseline;
2. OxideBatch with equivalent semantics;
3. comparable external framework such as Spring Batch.

Do not trade transaction durability, checkpoint semantics, validation, or resource bounds for a better benchmark number. Do not put large stress matrices in normal PR CI unless their signal justifies the cost.

## Diagnostics, security, and privacy

- Preserve actionable error category/context as far as the consumed public API permits. If the framework erases root-cause provenance, document and backlog that limitation rather than bypassing its abstraction unsafely.
- Never log raw CSV rows, names, email addresses, business payloads, credentials, connection strings with passwords, bound SQL values, or other sensitive input data.
- Synthetic identities must use reserved/non-real data such as `.test` email domains.
- No production credentials or third-party secrets belong in fixtures, evidence, workflow files, issues, or logs.
- Use least-privilege GitHub Actions permissions. Pin third-party actions to immutable commit SHAs when adding or materially revising workflows.

## Rust quality bar

- Keep expected failures in `Result`; no panic-based ordinary control flow.
- `unwrap`/`expect` in production paths require a narrowly documented invariant; prefer typed propagation/context.
- Keep public/workload-specific types small and explicit. Avoid stringly typed lifecycle state and magic constants where a type or named configuration is clearer.
- New dependencies must justify maintenance, MSRV, compile-time, supply-chain, and feature cost.
- Preserve the workload's declared MSRV unless an intentional, documented change is reviewed.
- Keep formatting and Clippy warning-free; use `--locked` for reproducibility-sensitive Cargo commands.

## Tests and CI

CI green is necessary but never sufficient for merge approval. Tests must cover the semantic risk introduced by the change.

For `csv-postgres`, preserve the existing clean, malformed-input, write-failure, commit-boundary, hard-crash/restart, duplicate/idempotency, final-state equivalence, input-mutation, verifier-negative-control, and resource-sanity coverage unless a reviewed contract explicitly changes.

When changing restart, transaction, verifier, failure-injection, evidence, or database behavior, run the relevant real PostgreSQL integration tests. Do not replace them with mocks if the contract under review depends on database transactions or process recovery.

Do not claim a command/test passed unless it was actually run. State skipped checks and why.

## Framework findings

A workload finding must be classified accurately: framework bug, API ergonomics, missing capability, documentation gap, performance issue, or semantics/contract ambiguity. A framework issue should contain a minimal reproducer or precise public-API limitation and should not exaggerate the workload observation into a broader guarantee.

Do not modify the workload solely to conceal a framework defect. Keep temporary workarounds local, obvious, documented, and removable.

## Scope discipline

Keep PRs focused. A workload validation PR must not casually expand into framework development, scheduler work, distributed execution, dashboards, Kafka/S3 support, parallel partitioning, or unrelated benchmark campaigns.

Preserve unrelated user changes. Prefer one writer for overlapping code areas; use subagents for bounded exploration/review rather than concurrent edits to the same files.

## Strict review and definition of done

Review the exact final HEAD, not an earlier commit and not CI status alone. Review correctness, architecture/ownership, abstractions and duplication, error handling and diagnostics, API/version provenance, performance/resource retention, dead code/hacks, edge cases, transaction/restart semantics, tests, diff scope, documentation, and evidence consistency.

A task is done only when:

- the exact published dependency and lockfile provenance are correct;
- implementation and tests agree with the documented contract;
- failure/restart behavior is exercised at the relevant boundaries;
- verification can independently detect incorrect final state;
- resource behavior remains appropriate to the workload claim;
- framework limitations are recorded rather than hidden;
- evidence and documentation are reproducible and non-exaggerated;
- required CI is green for the exact reviewed HEAD.

Any HEAD change invalidates a previous strict-review PASS.