# oxide-batch-workloads

Independent, external consumer applications that validate published
[OxideBatch](https://github.com/luceat-lux-vestra/oxide-batch) releases —
real workloads built against the **crates.io artifact**, never a local
checkout of the framework, to answer with evidence whether a release's
public API is actually usable and what its guarantees really are.

This is separate from OxideBatch's own repository on purpose: a real
external consumer doesn't get to fix framework bugs by editing the
framework's source, and neither does this one. A defect found here becomes
an issue against `luceat-lux-vestra/oxide-batch`, never a source change in
this repository.

## Validation program

See [`ROADMAP.md`](ROADMAP.md) for the workload-driven validation program:
item I/O, processing/failure semantics, database portability, comparative
benchmarks, scalability, scheduler/orchestrator interoperability, event-driven
launch and completion feedback, control-plane/API interoperability,
observability, deployment lifecycle, configuration/security, upgrade/DR, and
extension/test-kit usability.

The roadmap describes evidence targets, not promises to embed external systems
inside OxideBatch. Specific schedulers, brokers, dashboards, secret stores, and
orchestration platforms remain external unless workload evidence demonstrates a
missing framework-neutral contract.

## Workloads

| Path | Release validated | Purpose |
|---|---|---|
| [`csv-postgres/`](csv-postgres/) | `oxide-batch` `0.6.0` | Streaming CSV → PostgreSQL restartable batch import: transaction/checkpoint/restart semantics, crash recovery, application-level idempotency, resource bounds. |
| [`postgres-postgres/`](postgres-postgres/) | `oxide-batch` `0.6.0` | PostgreSQL → PostgreSQL cursor + keyset/paging restartable transform (campaign #63): deterministic source identity, released enlisted batch writer, independent streaming verification, and rollback + real hard-crash/new-process recovery for both reader modes. Retained larger-dataset resource evidence is the final campaign slice. |

## Accepted comparative benchmark evidence

**Correctness gates performance claims.** A candidate enters a primary
performance comparison only after its clean execution, transaction/checkpoint
boundary, hard-death recovery, and final-state equivalence satisfy the selected
semantic comparison class. A faster run with weaker durability is not accepted
as a comparable result.

Campaign [#79](https://github.com/luceat-lux-vestra/oxide-batch-workloads/issues/79)
produced the accepted four-way PostgreSQL → PostgreSQL same-runner observation.
The retained canonical run is
[workflow run 34045256715](https://github.com/luceat-lux-vestra/oxide-batch-workloads/actions/runs/34045256715),
at exact `main` SHA `e7b7e31bd12890ebb5a13f32acb22bb66019ccde`.

Canonical configuration and runtime provenance:

- runner: `ubuntu-24.04`;
- PostgreSQL: `18.6`;
- rows: `1,000,000`, seed `20260904`;
- chunk size: `1000`;
- cursor fetch size: `500`;
- paging page size: `750`;
- warmups: `2` per candidate/mode;
- measured rounds: `8` per candidate/mode;
- raw Rust: sqlx `0.9.0`;
- OxideBatch: exact published `0.6.0`;
- raw Java: pgjdbc `42.7.13`;
- Spring Batch: `6.0.5`;
- Java: Temurin/OpenJDK `25.0.4.1` LTS;
- Rust: `rustc` / `cargo` `1.98.1`.

The table below reports **paired median elapsed ratios (`B/A`)** from the
retained report. These are not ratios of aggregate medians and not a
single-run winner.

| Comparison | Cursor | Paging | Interpretation |
|---|---:|---:|---|
| raw Rust → OxideBatch | `1.2455×` | `1.4052×` | Rust-side framework/lifecycle attribution |
| raw Java → Spring Batch | `2.2815×` | `1.9718×` | JVM-side framework/lifecycle attribution |
| raw Rust → raw Java | `1.0343×` | `1.0373×` | Runtime/driver/system observation only |
| OxideBatch → Spring Batch | `1.8692×` | `1.4669×` | Product-level same-host observation; language/runtime effects remain |

Artifact and report integrity for that accepted run:

- artifact: `postgres-postgres-four-way-34045256715-1`;
- artifact ID: `9993337888`;
- artifact SHA-256: `57044dc37ae29794d3f81c5eb9641a4bc4d4d532f80e5cde85a8721673d35f4b`;
- report SHA-256: `9210f56606ea99e3d25d4f0fe8174247bdf9b8acad60ddbd75c1d09fc5dd30a2`.

These hosted-runner numbers are **observational evidence**, not a protected
numeric threshold and not an unqualified “X% faster/slower” marketing claim.
Retained limitations include hosted-runner variability, non-isolated Spring
bootstrap cost beyond the recorded launcher/bootstrap boundary, cross-language
startup/runtime effects, and the absence of symmetric transaction-count
instrumentation where that instrumentation would perturb the measurement.

### Semantic qualification can exclude a candidate before benchmarking

Campaign [#86](https://github.com/luceat-lux-vestra/oxide-batch-workloads/issues/86)
qualified JBeret `3.2.0.Final` on Java 25 / PostgreSQL 18 before allowing a
performance stage. Clean cursor and paging execution passed, but external
SIGKILL after the workload writer's business commit exposed a durable-boundary
mismatch in both modes: business rows had advanced to `300` while JBeret's
durable reader/write checkpoint remained at `200`. Public
`JobOperator.restart(...)` then replayed from customer `201` and failed on the
already-committed destination primary key.

Result: JBeret `3.2.0.Final` **fails `semantic-parity-minimal-durability` for
this workload and is reference-only**. No primary JBeret performance comparison
was run, and no numeric JBeret performance claim is made.

Each workload is a standalone Cargo project with its own `Cargo.lock`
pinned to a published `oxide-batch = "=X.Y.Z"` (registry source, verifiable
in the lockfile — never a path/git dependency), its own CI, and its own
evidence under `validation/`.

[`workloads.json`](workloads.json) is the canonical inventory used by repository
controls to determine which top-level Cargo projects are validation workloads.
A top-level Cargo project that is repository-owned tooling rather than a
validation workload must be listed explicitly as a reserved project with a
non-empty rationale; it must not be silently omitted from the inventory.

## CI: registry-driven aggregate gates

The merge-gate workflow (`.github/workflows/ci.yml`) never hardcodes a
workload's build/test/service commands. It fans out over every registered entry
and invokes a small, stable, workload-owned contract (`<workload>/ci/validate`)
— see [`.github/WORKLOAD_CONTRACT.md`](.github/WORKLOAD_CONTRACT.md) for the
full contract, including the structural separation between real `workloads`
and bounded CI `fixtures` and the per-entry MSRV policy resolved from each
`Cargo.toml`.

The protected stable workload contexts are `workloads-ci` and
`workloads-msrv`, computed by `.github/scripts/aggregate_verdict.py`; the old
single-workload compatibility contexts `ci` and `msrv` were removed after the
staged ruleset migration completed. `supply-chain` is also a protected stable
aggregate for every registered real workload's locked dependency graph, while
`dependency-review` remains separately required for its distinct diff-scoped
dependency-change coverage.

The live `Protect main` ruleset therefore requires these four stable contexts:

- `dependency-review`
- `workloads-ci`
- `workloads-msrv`
- `supply-chain`

Per-workload shard job names are implementation details and are never branch
protection contracts.

## Adding a workload

A new workload gets its own top-level directory, its own `ci/validate`
contract entrypoint, and an entry under `workloads` (never `fixtures`) in
[`workloads.json`](workloads.json) declaring its MSRV policy (resolved from
its own `Cargo.toml`, never duplicated in the registry). It does not touch
another workload's dependency version, database schema, or CI
implementation.
Repository-level discovery must fail closed until the new project is
registered. See `csv-postgres/README.md` for what a workload's own
documentation should cover (quickstart, schema, restart semantics actually
observed, findings, resource notes, evidence reproduction).
