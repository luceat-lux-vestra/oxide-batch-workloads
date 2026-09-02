# Workload CI contract

This document defines the stable contract between the central merge-gate workflow
(`.github/workflows/ci.yml`) and registered entries in
[`workloads.json`](../workloads.json). The central workflow owns orchestration;
each workload owns its domain-specific validation implementation.

## Canonical registry

`workloads.json` is the source of truth for repository validation subjects. Its
schema version is 3 and it separates real workloads from bounded CI fixtures:

```json
{
  "schema_version": 3,
  "workloads": [
    { "name": "csv-postgres", "path": "csv-postgres", "msrv": { "declared": true } }
  ],
  "fixtures": [
    {
      "name": "fixture-heterogeneous",
      "path": "fixture-heterogeneous",
      "msrv": { "declared": false, "policy_reason": "<non-empty reason>" }
    }
  ],
  "reserved_top_level_cargo_projects": []
}
```

Repository discovery and registry validation fail closed on missing, duplicate,
ambiguous, unregistered, or invalid entries and on an invalid zero-workload
state.

### Workloads

Entries under `workloads` are real OxideBatch external validation subjects.
They are subject to exact published dependency provenance and repository-wide
supply-chain policy.

`.github/scripts/validate-oxidebatch-provenance.py` reads real workloads only
and enforces that directly declared first-party `oxide-batch` / `oxide-batch-*`
dependencies are exact published crates.io artifacts. Path/git/patch/source
replacement and other mechanisms that would substitute a non-published
validation subject are rejected.

### Fixtures

Entries under `fixtures` exist only to prove that central CI is contract-driven
across heterogeneous Cargo projects. They participate in the ordinary `ci` and
`msrv` fan-out/aggregate path but make no OxideBatch capability claim and are
not supply-chain validation subjects.

This is not a provenance escape hatch. Fixture manifests and lockfiles are
validated to contain **no** first-party `oxide-batch` / `oxide-batch-*` package
in the resolved graph. A fixture that needs an OxideBatch dependency must move
to `workloads` and receive the full provenance/evidence treatment.

A top-level repository-owned Cargo project that is neither a workload nor a
fixture must be listed under `reserved_top_level_cargo_projects` with a
non-empty rationale.

## What central CI knows

The central workflow knows only:

1. how to validate and discover registered entries;
2. that each registered entry is a Cargo project with an executable
   `<entry>/ci/validate`;
3. how to invoke the `ci` and, when applicable, `msrv` stages;
4. how to resolve each entry's MSRV from that entry's own `Cargo.toml`;
5. how to collect normalized shard results and compute fail-closed aggregate
   verdicts; and
6. for real workloads, how to invoke the canonical supply-chain validator.

It does **not** know PostgreSQL, `DATABASE_URL`, migrations, broker/object-store
configuration, workload smoke commands, or workload-name-specific branches.

## MSRV policy

`Cargo.toml` is the only source of truth for an entry's Rust version. The
registry never duplicates the version string.

An entry declares one of:

```json
{ "msrv": { "declared": true } }
```

or:

```json
{ "msrv": { "declared": false, "policy_reason": "<non-empty reason>" } }
```

Rules:

- `declared: true` requires `package.rust-version` in that entry's
  `Cargo.toml`.
- `declared: false` requires `package.rust-version` to be absent and requires a
  non-empty policy reason.
- a duplicated registry version field is invalid.
- an undeclared-MSRV entry still produces an explicit `msrv` shard result with
  outcome `not-applicable`; it is never silently omitted from coverage.

## Workload-owned `ci/validate`

Every registered entry exposes an executable `ci/validate` relative to its own
root.

### `ci/validate ci`

Runs the entry's full required validation. The entry owns its own formatting,
linting, build, services/bootstrap, migrations, tests, smoke scenarios, and
cleanup. The central workflow does not duplicate these commands.

### `ci/validate msrv`

For entries with `msrv.declared: true`, central CI first installs the
`Cargo.toml`-resolved toolchain and then invokes the entry's MSRV validation.
An entry with `msrv.declared: false` does not need to implement this stage.

The contract is the process exit code. The central workflow records shard
identity and normalized outcome from trusted matrix/registry data; workload
scripts do not self-author their own authoritative identity or verdict.

## Aggregate verdicts

`.github/scripts/aggregate_verdict.py` computes the stable aggregate results.
It verifies complete expected coverage and policy-correct outcomes rather than
only checking whether some successful job exists.

| stage | policy | required outcome |
|---|---|---|
| `ci` | all registered entries | `validated` |
| `msrv` | `declared: true` | `validated` |
| `msrv` | `declared: false` | `not-applicable` |
| `supply-chain` | every real workload | `validated` |

Discovery failure, failed/cancelled/skipped shards, missing or duplicate
results, unexpected extra results, malformed result files, policy/outcome
mismatch, and incomplete coverage all fail closed.

## Protected stable contexts

The staged migrations from #28 and #32 are complete. The live `Protect main`
ruleset requires exactly these stable contexts:

- `dependency-review`
- `workloads-ci`
- `workloads-msrv`
- `supply-chain`

The former compatibility contexts `ci` and `msrv` have been removed. Per-entry
shard names are implementation details and must never be registered as required
branch-protection contexts.

`dependency-review` remains separate because it protects dependency changes in
the PR diff. It is not a substitute for the full-current-graph `supply-chain`
control.

## Repository-wide supply-chain policy

Root [`deny.toml`](../deny.toml) is the canonical advisories/licenses/bans/source
policy. The workflow installs the repository-pinned cargo-deny version and runs
`.github/scripts/validate-supply-chain.py` independently for every registered
real workload against that workload's committed `Cargo.lock`.

The validator:

- resolves workload names through the canonical registry;
- accepts `workloads` entries only, never fixtures or arbitrary paths;
- requires the workload manifest and lockfile;
- invokes cargo-deny with `--locked --all-features` against the canonical root
  policy; and
- propagates the real scan result into the aggregate gate.

The source policy is fail-closed: only the approved crates.io source is trusted;
unknown registries and unapproved git sources are denied. Policy exceptions are
not workload-level opt-outs. Any future exception must be narrowly scoped in
`deny.toml`, documented next to the exception, and reviewed as a policy change.

`supply-chain` and `dependency-review` intentionally coexist:

- `dependency-review` is diff-scoped and evaluates newly introduced dependency
  changes on a PR;
- `supply-chain` re-evaluates each real workload's entire committed dependency
  graph under the canonical policy on every PR.

Neither replaces the other.

## Current fixture

`fixture-heterogeneous/` exists only to prove that the `ci`/`msrv` orchestration
works for an entry with a materially different shape from `csv-postgres` and
without workload-name branching in central CI. It has no OxideBatch dependency,
no production evidence claim, and no supply-chain workload status.

## Contract changes

A change to registry shape, protected stable contexts, provenance semantics,
MSRV semantics, aggregate verdict rules, or supply-chain scope is a repository
contract change. Such changes require exact-final-HEAD review and, when live
required contexts change, staged producer verification followed by ruleset
readback before obsolete protection is removed.
