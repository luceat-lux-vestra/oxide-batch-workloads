# Workload CI contract

This document defines the smallest stable contract between the central merge-gate
workflow (`.github/workflows/ci.yml`) and each registered entry in
[`workloads.json`](../workloads.json). It exists so the central workflow never
needs workload-name conditionals, database/broker/object-store knowledge, or
domain-specific commands.

## What the central workflow knows

1. The canonical registry (`workloads.json`) and how to validate/discover it
   (`.github/scripts/validate-workload-registry.py`,
   `.github/scripts/discover-workloads.py`).
2. That every registered entry is a Cargo project (enforced by registry
   validation) and exposes an executable at `<entry>/ci/validate`.
3. How to invoke that executable for exactly two stages: `ci` and `msrv`.
4. How to install a Rust toolchain at a version resolved from that entry's
   own `Cargo.toml` (`package.rust-version`), when its registry entry
   declares `msrv.declared: true`.
5. How to collect one normalized result per shard and compute an aggregate
   verdict (`.github/scripts/aggregate_verdict.py`), including checking that
   each shard's outcome actually matches what that entry's own registered
   MSRV policy requires -- not merely that the shard job "succeeded".

The central workflow never knows PostgreSQL, `DATABASE_URL`, migration
commands, a workload's smoke/test command sequence, or any future
database/broker/object-store topology. It never branches on an entry's
`name`.

## Registry schema (schema version 3): workloads vs. fixtures are structurally separate

`workloads.json` has two entry arrays, not one:

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

- **`workloads`** are real OxideBatch validation subjects.
  `.github/scripts/validate-oxidebatch-provenance.py` (#29) only ever reads
  this key -- `load_workloads()` never looks at `fixtures` at all. There is
  therefore no field a workload entry can set to weaken #29's exact
  published-provenance enforcement; that guarantee is structural, not a
  boolean toggle. An earlier draft of this contract added a
  `provenance.required: false` escape hatch to exempt a fixture from that
  rule; strict review correctly rejected it as a generic weakening any
  future real workload could also set, and it was replaced by this
  structural array split instead.
- **`fixtures`** are bounded, non-product CI-orchestration proofs (see
  below). They still go through the identical `ci/validate`
  fan-out/aggregate machinery as `workloads` -- that shared path is what
  proves the machinery is contract-driven -- but `validate-oxidebatch-provenance.py`
  never treats a fixture as a provenance subject requiring #29's exact-version
  enforcement.

  This is a two-sided structural guarantee, not merely "the validator
  doesn't look here": `validate-oxidebatch-provenance.py` *does* read every
  `fixtures` entry's `Cargo.toml` **and** `Cargo.lock`, specifically to
  enforce the opposite invariant -- a fixture must have **zero** first-party
  `oxide-batch`/`oxide-batch-*` presence anywhere in its resolved dependency
  graph. Without that check, registering an actual OxideBatch consumer under
  `fixtures` instead of `workloads` would itself become a live #29 bypass --
  a classification escape hatch replacing the boolean one. A fixture that
  ever needs an OxideBatch dependency is no longer a fixture; move it to
  `workloads` and give it full provenance and evidence treatment.

  The manifest check alone is not enough: Cargo workspace dependency
  inheritance (`{ workspace = true }`) resolves the real package from
  `[workspace.dependencies]`, never named in the text the manifest check
  reads, and a local/path helper crate can depend on OxideBatch without the
  fixture's own manifest ever mentioning it. `Cargo.lock` is the resolved
  ground truth regardless of how a package got there, so
  `validate_fixture_lockfile` additionally rejects any `[[package]]` entry
  whose `name` matches `oxide-batch`/`oxide-batch-*`, independent of what
  the manifest declares.

Both arrays share the same per-entry shape: `name`, `path`, `msrv`.

## MSRV: Cargo.toml is the only source of truth

`workloads.json` never duplicates a version string. Each entry's `msrv`
field is one of:

```json
{ "msrv": { "declared": true } }
```
```json
{ "msrv": { "declared": false, "policy_reason": "<non-empty human-readable reason>" } }
```

`validate-workload-registry.py` resolves the actual MSRV by reading that
entry's own `Cargo.toml` `package.rust-version`, and fails closed on any
inconsistency:

- `msrv.declared: true` requires `package.rust-version` to be present in
  Cargo.toml (the resolved version is what the central workflow installs
  and what `discover-workloads.py` emits into the fan-out matrix).
- `msrv.declared: false` requires `package.rust-version` to be **absent**
  from Cargo.toml -- a package that actually declares an MSRV can never be
  registered as policy-exempt.
- `msrv.declared: true` combined with an explicit `version` field in
  `workloads.json` is rejected outright: duplicating the version in two
  places is exactly the drift this design forbids (bump Cargo.toml's
  `rust-version` and the registry's resolved value moves with it
  automatically; there is nothing else to update).

A workload with no MSRV policy (`msrv.declared: false`) still gets a real
`msrv-shard` job on every PR -- it is never silently excluded from the
matrix. That shard emits an explicit `outcome: "not-applicable"` result
carrying the registry's `policy_reason`, and the aggregate verdict treats a
declared/not-declared mismatch (in either direction) as a hard failure, not
merely a missing result -- see "Aggregate verdict is policy-aware" below.

## The `ci/validate` entrypoint

Every registered entry must ship an executable file at `ci/validate`
(relative to its own top-level directory). It is invoked with that
directory as the current working directory and exactly one positional
argument:

- `ci/validate ci` -- the entry's full required validation: formatting,
  linting, build, guards, any services/bootstrap it needs (owned entirely by
  the entry itself -- e.g. via its own `docker-compose.yml`), its real test
  suite, and its golden-path smoke check. The script owns setup **and**
  teardown of anything it starts.
- `ci/validate msrv` -- invoked only when the entry's registry entry
  declares `msrv.declared: true`, after the central workflow has already
  installed the Cargo.toml-resolved toolchain as the active `rustc`/`cargo`.
  The script performs whatever build the entry considers its MSRV guarantee
  (at minimum, a locked build of all targets).

The contract is intentionally an exit code: `0` means the stage passed,
anything else means it failed. The central workflow captures the exit code
itself and writes the normalized shard result from trusted matrix data (the
entry's registered `name` and the invoked `stage`) -- it never trusts a
workload script to self-report its own identity or outcome in a structured
payload. Diagnostics belong in the script's stdout/stderr, which is captured
in the job log as usual.

An entry with `msrv.declared: false` does not need to implement the `msrv`
argument at all: the central workflow never invokes it, and instead
directly emits an explicit `not-applicable` shard result carrying the
registry's mandatory `msrv.policy_reason`.

## Aggregate verdict is policy-aware, not just presence-aware

`.github/scripts/aggregate_verdict.py`'s `compute_verdict` does not merely
check "does a successful result exist for every expected name". For every
expected shard it also computes the *required* outcome from that entry's
own registered MSRV policy (`expected_outcome_for`) and fails closed on any
mismatch:

| stage | msrv.declared | required outcome | any other outcome |
|---|---|---|---|
| `ci`   | (either)  | `validated`       | fails closed |
| `msrv` | `true`    | `validated`       | fails closed (e.g. an accidental `not-applicable` never passes) |
| `msrv` | `false`   | `not-applicable`  | fails closed (e.g. `validated` never passes) |

This is what prevents a workload that actually declares an MSRV from ever
being satisfied by a stray `not-applicable` disposition, and vice versa,
even though both are `status: "success"` at the shard level.

## Protected aggregate contexts

Issue #28's staged ruleset migration is complete. The stable workload merge
gates are now the aggregate contexts `workloads-ci` and `workloads-msrv`.
The temporary compatibility jobs named `ci` and `msrv`, which existed only
to keep the previous required contexts producible during migration, have
been removed.

`dependency-review` remains a separate required context because it protects
a distinct diff-scoped dependency-change surface; it is not an alias for the
registry-driven full workload aggregates.

The aggregate context names are the protection contract. Per-workload shard
job names are implementation details and must not be registered as required
contexts.

`supply-chain` (see below) is intended to become a third protected aggregate
context once it has emitted successfully on a real PR under strict review.
It is not yet registered in the live branch-protection ruleset -- that
migration is staged separately, the same way #28 staged `workloads-ci` and
`workloads-msrv` before requiring them.

## Repository-wide supply-chain policy (#32)

Root [`deny.toml`](../deny.toml) is the single canonical supply-chain policy
for this repository, enforced with [cargo-deny](https://github.com/EmbarkStudios/cargo-deny)
(version pinned exactly -- see `cargo install cargo-deny --version "=0.20.2"
--locked` in `.github/workflows/ci.yml`). It covers four policy classes:
advisories, licenses, bans, and sources. The source policy is fail-closed by
design: only the canonical crates.io registry is trusted, and there is no
git source, of any origin, pre-authorized -- unknown registries and unknown
git sources are both denied outright rather than merely warned about.

**Scope: `workloads`, never `fixtures`.** Supply-chain scanning is a
full-current-dependency-graph production control, structurally distinct
from `ci`/`msrv`'s shared fan-out. `.github/scripts/discover-supply-chain-workloads.py`
is a separate, narrower discovery projection from `discover-workloads.py`:
it selects only `workloads.json`'s `workloads` array and never `fixtures`.
A fixture participating in the ordinary `ci`/`msrv` machinery is not, and
must never become, an equivalent-weight supply-chain scanning subject.

**Each workload is checked independently, against its own locked graph.**
There is one canonical policy (`deny.toml`), but no single root-level
cargo-deny invocation stands in for every workload. For every registered
real workload, the central workflow runs a dedicated `supply-chain-shard
(<workload>)` job that is conceptually equivalent to:

```sh
cargo deny --config deny.toml --manifest-path <workload>/Cargo.toml \
  --locked --all-features check advisories licenses bans sources
```

`--locked` means a missing lockfile, a stale lockfile, or any attempt at
dependency re-resolution fails the shard outright -- the scan always
operates on the exact graph actually committed to the repository, never a
graph cargo is allowed to re-resolve on the fly.

**The repository-owned validator, not the workflow, implements the
semantics.** `.github/scripts/validate-supply-chain.py` is what actually
turns a canonical registry *name* into that cargo-deny invocation: it loads
and validates the registry through the existing `validate-workload-registry.py`,
resolves the requested name against `workloads` only (rejecting a fixture
name or an arbitrary path outright), verifies the manifest/lockfile are
present, and then propagates cargo-deny's own exit code verbatim as the
shard's pass/fail signal. The GitHub workflow is a thin, workload-name-agnostic
caller of this script, not a second implementation of the scan -- #33's
planned scheduled advisory-drift audit is expected to install the same
pinned cargo-deny version and invoke this exact script per workload, rather
than reimplementing it. (#33 does not exist yet as of this document; nothing
here should be read as claiming scheduled auditing is already wired up.)

**`supply-chain` is the stable aggregate, and it is policy-uncompromising.**
Unlike `msrv`, the `supply-chain` stage has no policy-exemption concept
analogous to an undeclared MSRV: every registered real workload is always
expected to report the `validated` outcome, full stop. The aggregate is
computed by the same `aggregate_verdict.py` used by `workloads-ci` and
`workloads-msrv` (extended with a third `supply-chain` stage, not a second
parallel implementation), so it fails closed on exactly the same class of
incomplete-coverage conditions: a missing, cancelled, skipped, duplicate, or
unexpected-extra result; a malformed result file; a failed discovery step;
or a failed fan-out job.

**Why both `supply-chain` and `dependency-review` exist.** They protect
different surfaces and neither can substitute for the other.
`dependency-review` (`.github/workflows/dependency-review.yml`) is diff-scoped:
it flags newly introduced dependency changes in a given PR against GitHub's
advisory data, and stays a required context in its own right. `supply-chain`
is a full-current-graph control: it re-evaluates every registered real
workload's entire locked dependency graph against the canonical policy on
every PR, catching pre-existing graph issues a diff-scoped review would
never see (for example, a license or advisory finding introduced by an
upstream release with no dependency change in the PR at all, or a graph
that predates `dependency-review` being enabled). Removing either would
leave a real gap the other does not cover.

**Policy exceptions: none, as of this writing.** `deny.toml`'s `ignore`,
`exceptions`, `allow`/`deny` (bans), and similar lists are all empty. If a
future real workload genuinely requires one, it must be scoped as tightly
as the cargo-deny schema allows (an exact advisory ID, crate, version, or
source -- never a blanket suppression), carry a concrete reason comment
directly in `deny.toml` next to the entry, and never take the form of a
generic `ignore = true`, a workload-level opt-out field, a registry escape
hatch, an environment-variable bypass, or `continue-on-error`.

## Why a fixture workload exists

`fixture-heterogeneous/` is not a validation workload and makes no OxideBatch
evidence claim. Its only purpose is to prove, in real CI, that the central
workflow's fan-out is contract-driven: it has no PostgreSQL, no services, a
materially different `ci/validate` implementation, `msrv.declared: false`,
and it is registered under `fixtures`, not `workloads` -- so it is
structurally, not just declaratively, outside #29's provenance scope. It
participates in the exact same fan-out/aggregate machinery as `csv-postgres`
with zero workload-name branching in the central workflow.
