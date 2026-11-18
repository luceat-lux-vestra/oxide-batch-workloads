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
across heterogeneous Cargo projects. They participate in the ordinary `ci`
fan-out and the full MSRV aggregate authority, but an entry with
`msrv.declared=false` does not consume a dedicated MSRV runner. Its explicit
`not-applicable` evidence is synthesized from validated registry policy inside
the stable aggregate job. Fixtures make no OxideBatch capability claim and are
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
5. how to collect normalized CI/MSRV shard results and compute fail-closed
   aggregate verdicts; and
6. for real workloads, how to cross-check the supply-chain discovery projection
   against the canonical registry and invoke the canonical supply-chain validator
   for every real workload, which may dispatch a workload-owned
   additional-ecosystem hook without teaching the central workflow that
   ecosystem's policy.

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
- an undeclared-MSRV entry still produces explicit normalized `msrv` evidence
  with outcome `not-applicable`, synthesized by the aggregate authority from
  its validated registry `policy_reason`; it is never silently omitted from
  coverage and does not require a no-op runner.

## Workload-owned `ci/validate`

Every registered entry exposes an executable `ci/validate` relative to its own
root.

### `ci/validate ci`

Runs the entry's full required validation. The entry owns its own formatting,
linting, build, services/bootstrap, migrations, tests, smoke scenarios, and
cleanup. The central workflow does not duplicate these commands.

### `ci/validate msrv`

For entries with `msrv.declared: true`, central CI places the entry in the
declared-MSRV execution projection, installs the `Cargo.toml`-resolved
toolchain, and invokes the entry's MSRV validation. An entry with
`msrv.declared: false` does not enter the runner matrix and does not need to
implement this stage.

The contract is the process exit code. The central workflow records shard
identity and normalized outcome from trusted matrix/registry data; workload
scripts do not self-author their own authoritative identity or verdict.

## Aggregate verdicts and supply-chain authority

`.github/scripts/aggregate_verdict.py` computes the stable aggregate results.
It verifies complete expected coverage and policy-correct outcomes rather than
only checking whether some successful job exists.

| stage | policy | required outcome |
|---|---|---|
| `ci` | all registered entries | `validated` |
| `msrv` | `declared: true` | `validated` |
| `msrv` | `declared: false` | `not-applicable` |

For `supply-chain`, the stable authority is intentionally not an artifact
fan-out aggregate. One runner independently compares discovery's real-workload
projection with the canonical registry, rejects missing/extra/duplicate/malformed
subjects, and then validates every real workload sequentially. Any Cargo policy
failure, required additional-ecosystem hook failure, or completeness mismatch
fails the stable `supply-chain` authority.

For MSRV, the runner matrix contains only `declared=true` entries while the
aggregate expectation remains the complete canonical discovery set. Before the
verdict runs, the aggregate job synthesizes explicit `not-applicable` results
for every validated `declared=false` entry. Malformed policy, duplicate names,
or collisions with downloaded shard results fail closed.

For CI/MSRV, discovery failure, failed/cancelled/skipped required shards,
missing or duplicate results, unexpected extra results, malformed result files,
policy/outcome mismatch, and incomplete coverage all fail closed.

## Protected PR merge authority

The PR `CI` workflow hosts the exact existing `dependency-review` check,
two artifact-verifying aggregates (`workloads-ci` and `workloads-msrv`), the
single-runner `supply-chain` authority, and a final fail-closed `Merge Gate`
job. The final job requires discovery, every applicable CI/MSRV shard and
aggregate, the supply-chain authority, and all scoped semantic authorities to
succeed, and requires `dependency-review` for PR events. A draft PR is not considered
validated; manual CI dispatch does not invent a dependency-diff review.
The CI/declared-MSRV per-shard artifact completeness and full-registry MSRV
policy/outcome checks remain owned by `aggregate_verdict.py`; undeclared-MSRV
evidence materialization is fail-closed and registry-driven before that verdict.
Supply-chain completeness is owned by `run-supply-chain-authority.py`, not by
the coarse final gate.

The former contexts `ci` and `msrv` are obsolete; per-entry shard names are
implementation details, never standalone branch-protection requirements.
Before the live Ruleset #21944159 is changed, it still requires the previous
four contexts (`dependency-review`, `workloads-ci`, `workloads-msrv`, and
`supply-chain`). Retain all four until the new final `Merge Gate` has
passed on the exact PR HEAD and merged protected main. Only then replace the
four required contexts with the observed `Merge Gate` check. Never remove
protection ahead of verifying the new producer.

`dependency-review` continues to protect dependency changes in the PR diff;
it is not replaced by full-current-graph `supply-chain` validation. The
independent push-only Fast branch workflow remains an **advisory early feedback**
producer; PR merge authority no longer polls or reuses its run. Registry
tests and validation run locally within `discover` before CI/declared-MSRV
fan-out and the single-runner supply-chain authority.

## Cargo lock consistency authority

Every canonical workload and fixture owns a committed `Cargo.lock`. Lockfile
correctness is determined by Cargo's resolver, never by a Git diff heuristic and
never by hand-editing package/dependency tables.

The canonical authority is:

```bash
python3 .github/scripts/validate-cargo-locks.py check
```

For each registered workload/fixture it executes full dependency resolution with
`cargo metadata --locked --all-features`. The authority intentionally does
**not** use `--no-deps`: that option can accept a stale lockfile when an
all-features optional dependency edge is missing from the resolved graph, while
a downstream full-graph `--locked` consumer correctly rejects the same file.
A manifest/feature/dependency change that requires Cargo to mutate the lockfile
therefore fails immediately with an explicit stale-lock diagnostic before
workload fan-out or supply-chain policy scanning.

When a dependency or feature graph intentionally changes, refresh exactly one
registered subject through Cargo itself:

```bash
python3 .github/scripts/validate-cargo-locks.py refresh --name postgres-postgres
```

The refresh path deliberately omits `--locked` for the first resolver pass so
Cargo owns the complete lockfile mutation, then immediately reruns the same
all-features metadata query with `--locked`. A refresh is successful only when
that second reproducibility check passes.

Do not edit `Cargo.lock` manually. Do not infer that a lockfile must or must
not change merely because `Cargo.toml` changed: optional dependencies and
feature edges are resolver semantics. Conversely, a legitimate lock-only update
remains valid when Cargo accepts it under the registered manifest.

`reserved_top_level_cargo_projects` are outside this authority until they
become canonical validation subjects; they may intentionally lack a committed
lockfile. Workloads and fixtures are never exempt.

Central CI runs the authority test suite and the real repository-wide lock
check in `discover` before supply-chain validation. This makes a stale lockfile
a first-class dependency-metadata failure rather than an incidental later
`cargo-deny --locked` failure.

## Repository-wide supply-chain policy

Root [`deny.toml`](../deny.toml) is the canonical advisories/licenses/bans/source
policy. The workflow installs the repository-pinned cargo-deny version once.
`.github/scripts/run-supply-chain-authority.py` independently cross-checks the
discovered real-workload projection against the canonical registry, then runs
`.github/scripts/validate-supply-chain.py` for every registered real workload
against that workload's committed `Cargo.lock`.

The validator:

- resolves workload names through the canonical registry;
- accepts `workloads` entries only, never fixtures or arbitrary paths;
- requires the workload Cargo manifest and lockfile;
- keeps root `deny.toml` exception-free and loads the separate
  `.github/supply-chain-advisory-exceptions.json` registry;
- validates every advisory exception against an exact registered workload,
  RustSec advisory ID, resolved package name, allowed
  `informational/unmaintained` classification, repository issue owner,
  non-empty reason, and removal condition;
- materializes a temporary effective deny config only for a workload that has
  declared exceptions; unaffected workloads use root `deny.toml` byte-for-byte;
- preserves `unused-ignored-advisory = "deny"`, so an exception that stops
  matching its declared workload fails closed and must be removed;
- invokes cargo-deny with `--locked --all-features` against that workload's
  effective policy;
- detects nested Maven `pom.xml` manifests and fails closed unless that workload
  exposes an executable `ci/validate-supply-chain` hook;
- invokes that workload-owned hook only after the Cargo policy succeeds; and
- propagates either Cargo-policy or additional-hook failure into the same stable
  `supply-chain` authority while continuing through the remaining registered
  workloads for complete diagnostics.

The additional hook is intentionally a narrow extension point, not a second central
implementation of Maven/Gradle/npm policy. Central CI detects that Maven exists and
requires a hook; the workload owns the Maven-specific exact-version, repository/source,
and dependency-boundary checks. Ordinary `ci/validate ci` remains responsible for
actually resolving/building/testing the nested ecosystem when its semantic contract
requires that. A future additional ecosystem must receive an explicit fail-closed
detection rule before relying on this hook rather than silently assuming it is covered.

The source policy is fail-closed: only the approved crates.io source is trusted;
unknown registries and unapproved git sources are denied. Policy exceptions are
not workload-level opt-outs and are never written directly into root
`deny.toml`. A workload-scoped advisory exception is a separate reviewed
policy record and must identify one exact workload, one exact RustSec advisory,
the resolved affected package, an allowed informational classification, a
repository issue owner, a concrete reason, and a removal condition.

The active `RUSTSEC-2026-0543` records are deliberately limited to the two
workloads whose committed lock graphs currently resolve `chrono`:
`csv-postgres` and `postgres-postgres`. RustSec marks every `chrono`
version affected and provides no patched version. The former uses `chrono`
for date/time + SQLx integration; the latter resolves it through the qualified
tcmalloc build-dependency path
(`tcmalloc-better -> libtcmalloc-sys -> patch -> chrono`). Replacing those
dependencies while the OxideBatch 0.6.0 / allocator evidence campaign is in
flight would alter the validation subject rather than apply a safe patch.

Issue #297 owns migration/removal. Each exception becomes invalid as soon as
its named workload no longer resolves `chrono`; the validator and
`unused-ignored-advisory = "deny"` jointly keep that lifecycle fail-closed.
`unmaintained = "all"`, unsound/yanked policy, all non-declared
informational advisories, and actual vulnerability advisories remain blocked.

`supply-chain` and `dependency-review` intentionally coexist:

- `dependency-review` is diff-scoped and evaluates newly introduced dependency
  changes on a PR;
- `supply-chain` re-evaluates each real workload's complete Cargo graph under
  `deny.toml` and any detected nested-ecosystem manifest policy through its required
  workload-owned hook on every PR.

Neither replaces the other.

## Current fixture

`fixture-heterogeneous/` exists only to prove that CI fan-out and full MSRV
aggregate policy remain registry-driven for an entry with a materially different
shape from `csv-postgres`, without workload-name branching in central CI. It has no OxideBatch dependency,
no production evidence claim, and no supply-chain workload status.

## Contract changes

A change to registry shape, protected stable contexts, provenance semantics,
MSRV semantics, aggregate verdict rules, or supply-chain scope is a repository
contract change. Such changes require exact-final-HEAD review and, when live
required contexts change, staged producer verification followed by ruleset
readback before obsolete protection is removed.
