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
workload's build/test/service commands. It fans out over every workload in
`workloads.json` and invokes a small, stable, workload-owned contract
(`<workload>/ci/validate`) — see
[`.github/WORKLOAD_CONTRACT.md`](.github/WORKLOAD_CONTRACT.md) for exactly
what that contract is, why `workloads.json` structurally separates real
`workloads` from bounded CI `fixtures`, and why MSRV is always resolved from
each entry's own `Cargo.toml` rather than duplicated in the registry. The
target stable branch-protection contexts are the aggregate verdicts
(`workloads-ci`, `workloads-msrv`), computed by a small testable tool
(`.github/scripts/aggregate_verdict.py`) rather than opaque workflow
expressions — never a per-workload job name. Live branch protection still
requires `ci`/`msrv` today; `ci.yml` keeps emitting those as thin
compatibility shims until the ruleset migration described in
`.github/WORKLOAD_CONTRACT.md` completes.

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
