# Campaign #79 four-way same-host measurement

This document defines PR4's measurement/report boundary for the PostgreSQL-to-PostgreSQL comparison. It extends the already accepted #73 raw-Rust/Oxide methodology with the raw Java/JDBC and Spring Batch 6.0.5 candidates added by #79 PR1-PR3.

No timing produced by PR validation, a feature branch, PR1, PR2, or PR3 is campaign performance evidence. Only a fresh canonical workflow run from authoritative `main` after PR4 is merged and post-merge correctness gates are green can be reviewed for #79 closure.

## Comparison and interpretation boundary

The comparison class remains `semantic-parity-minimal-durability`.

The four candidates are:

1. `raw_rust`: raw Rust/sqlx 0.9.0 control from #73;
2. `oxide`: exact published OxideBatch 0.6.0 workload;
3. `raw_java`: Java 21 + exact pgjdbc 42.7.13 direct-JDBC control;
4. `spring`: Java 21 + exact Spring Batch 6.0.5 + pgjdbc 42.7.13.

The meaningful attribution pairs are deliberately distinct:

- raw Rust vs OxideBatch: Rust framework/lifecycle attribution;
- raw Java/JDBC vs Spring Batch: JVM-side Spring Batch lifecycle/repository/checkpoint attribution;
- raw Rust vs raw Java/JDBC: runtime/driver/system observation, not a framework verdict;
- OxideBatch vs Spring Batch: product-level same-host observation. Language, runtime, and startup differences are not removed.

The report never collapses those boundaries into a single winner claim.

## Canonical configuration

The manual workflow is `.github/workflows/benchmark-postgres-postgres-four-way.yml` and is `workflow_dispatch` only.

Canonical inputs are:

| Parameter | Value |
|---|---:|
| rows | 1,000,000 |
| seed | 20260904 |
| chunk size | 1,000 |
| cursor fetch size | 500 |
| paging page size | 750 |
| warmup rounds per mode | 2 |
| measured rounds per mode | 8 |
| recovery observations | 1 per candidate and reader mode |

Measured rounds must be a multiple of four. Candidate order is a deterministic four-position rotation, so eight measured rounds place every candidate in each position exactly twice for each reader mode. Diagnostic overrides are bounded and recorded, but a non-canonical run is not acceptance evidence.

## Build-once and database isolation

All four candidates execute in one `ubuntu-24.04` GitHub-hosted job against the same PostgreSQL 18 service.

- Rust release binaries are built once before timed samples.
- The Maven reactor and Java runtime dependencies are built/resolved once.
- A JDK-only `BenchmarkLauncher` is compiled once and used only to expose the Java target-main active interval and exact candidate PID.
- JVM ambient tuning variables (`JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, and `JDK_JAVA_OPTIONS`) must be empty; concrete Java/JVM/Maven versions and command-line flags are retained before measurement.

The harness creates one deterministic template database from `template0`. OxideBatch migration establishes the canonical application/framework schemas, then benchmark-owned raw-Rust, raw-JDBC, and Spring metadata migrations are layered on, and the source is seeded once. Every warmup, measured, and recovery candidate sample receives a fresh clone of that template. Clone/setup/migration, seed, cleanup, and final independent verification are outside the primary timed interval.

## Clean samples

The timed clean interval is one fresh candidate process running its normal `run` command. Source-digest work stays inside the interval because it is part of the normal execution contract for all four candidates.

Every sample must independently pass `postgres-postgres verify` before it can enter a measured distribution. The harness also requires all four candidates in a round to report the same canonical source digest.

Per sample the report records end-to-end elapsed, throughput, user/system CPU, peak RSS, process identity, business/final-state observations, derived writer work, source/destination digests, exit status, and candidate artifact identity.

For Java candidates, `BenchmarkLauncher` records the interval immediately before target `main()` invocation until that `main()` returns. This provides a separate target-main active-work interval and a derived JVM/launcher pre-main component. It does **not** reliably isolate Spring application-context construction, so that value is explicitly `null`/`not-separately-observed`. No analogous startup subtraction is applied to cross-language product claims.

Summaries contain min/median/max/p95 distributions and per-round paired ratios for each declared interpretation pair. There is no hosted-runner numeric merge threshold.

## Recovery observations

For each reader mode and candidate, the harness performs one deterministic external hard-death observation at the pre-commit semantic boundary near 50%: business writes for the target chunk have executed but the chunk commit has not completed.

The child emits a PID-bearing semantic marker and waits. The parent sends `SIGKILL` to that exact PID. The harness then verifies the durable business prefix before starting any continuation.

- raw Rust/sqlx: continue by starting a new raw process;
- OxideBatch: time public `recover` separately, then start continuation in a new process;
- raw Java/JDBC: continue by starting a new JVM using the durable raw checkpoint;
- Spring Batch: time public `JobOperator.recover` separately, then restart via the normal public run/restart path in a third JVM.

Recovered processes must have identities distinct from the killed process (and from an operator-recovery process when one exists). Final output must pass the same independent Rust verifier. The report retains the durable resume position, reprocessed rows, and zero duplicate/skipped/lost counts.

Raw Java's before/after-commit crash semantics are additionally protected by `ci/validate-raw-jdbc-crash-recovery`, covering cursor and paging and source mutation fail-closed behavior. This closes the raw-Java hard-death obligation that remained after PR3's Spring-specific recovery work.

## Provenance and retained artifact

The JSON report records:

- exact GitHub SHA/run/attempt/runner identity;
- rustc/cargo and Java/javac/Maven versions;
- JVM command-line flags and ambient-option state;
- exact OxideBatch 0.6.0 and sqlx 0.9.0 Cargo.lock source/checksum records;
- exact Spring Batch 6.0.5 and pgjdbc 42.7.13 dependency-tree entries;
- Rust binary, Java JAR, launcher-class, and dependency-tree SHA-256 values;
- OS/kernel/CPU/memory observations;
- PostgreSQL configured image, server version, and image id;
- all effective benchmark inputs and explicit limitations.

The workflow uploads the report, its SHA-256 file, and both Java dependency trees even when the campaign command fails where safe. Artifact retention is 30 days. GitHub artifact id/digest and the report digest are reviewed and recorded in #79 after the authoritative-main run; they cannot be known before upload.

This is observational evidence, not manifest-v2 trusted producer attestation.

## Protected semantic validation

The expensive canonical campaign never runs on pull requests. Protected CI covers the measurement logic with bounded evidence instead:

- four-position rotation, balanced-round, statistics, parser, input, and workflow-security unit tests;
- raw-JDBC external-kill policy adversarial tests;
- real PostgreSQL raw-JDBC cursor/paging before/after-commit hard-death recovery;
- a 401-row four-way cursor+paging smoke with four measured rounds and all four recovery paths;
- existing repository CI continues to cover the previously accepted raw Rust/Oxide and Spring correctness/recovery obligations.

The 401-row smoke timings are discarded and must never be quoted as campaign performance results.
