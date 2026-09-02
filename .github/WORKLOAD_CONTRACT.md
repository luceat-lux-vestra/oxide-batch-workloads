# Workload CI contract

This document defines the smallest stable contract between the central merge-gate
workflow (`.github/workflows/ci.yml`) and each registered workload in
[`workloads.json`](../workloads.json). It exists so the central workflow never
needs workload-name conditionals, database/broker/object-store knowledge, or
domain-specific commands.

## What the central workflow knows

1. The canonical registry (`workloads.json`) and how to validate/discover it
   (`.github/scripts/validate-workload-registry.py`,
   `.github/scripts/discover-workloads.py`).
2. That every registered workload is a Cargo project (enforced by registry
   validation) and exposes an executable at `<workload>/ci/validate`.
3. How to invoke that executable for exactly two stages: `ci` and `msrv`.
4. How to install a Rust toolchain at a version taken from the registry's
   `msrv.version` field, when `msrv.declared` is `true` — the *value* is
   registry data, not a hardcoded workload name.
5. How to collect one normalized result per shard and compute an aggregate
   verdict (`.github/scripts/aggregate_verdict.py`).

The central workflow never knows PostgreSQL, `DATABASE_URL`, migration
commands, a workload's smoke/test command sequence, or any future
database/broker/object-store topology. It never branches on a workload's
`name`.

## The `ci/validate` entrypoint

Every registered workload must ship an executable file at `ci/validate`
(relative to the workload's own top-level directory). It is invoked with the
workload directory as the current working directory and exactly one
positional argument:

- `ci/validate ci` — the workload's full required validation: formatting,
  linting, build, guards, any services/bootstrap the workload needs (owned
  entirely by the workload — e.g. via its own `docker-compose.yml`), the
  workload's real test suite, and its golden-path smoke check. The script
  owns setup **and** teardown of anything it starts.
- `ci/validate msrv` — invoked only when the workload's registry entry
  declares `msrv.declared: true`, after the central workflow has already
  installed the declared toolchain as the active `rustc`/`cargo`. The script
  performs whatever build the workload considers its MSRV guarantee (at
  minimum, a locked build of all targets).

The contract is intentionally an exit code: `0` means the stage passed,
anything else means it failed. The central workflow captures the exit code
itself and writes the normalized shard result from trusted matrix data (the
workload's registered `name` and the invoked `stage`) — it never trusts a
workload script to self-report its own identity or outcome in a structured
payload. Diagnostics belong in the script's stdout/stderr, which is captured
in the job log as usual.

A workload with `msrv.declared: false` does not need to implement the `msrv`
argument at all: the central workflow never invokes it, and instead directly
emits an explicit `not-applicable` shard result carrying the registry's
mandatory `msrv.policy_reason`. This keeps "no MSRV" a visible, reviewed
policy statement rather than an accidental skip that aggregate logic could
mistake for success.

## Registry fields that back the contract

Each `workloads.json` entry (schema version 2) additionally declares:

```json
{
  "name": "csv-postgres",
  "path": "csv-postgres",
  "msrv": { "declared": true, "version": "1.95" },
  "provenance": { "required": true }
}
```

or, for a workload with no MSRV policy:

```json
{
  "msrv": { "declared": false, "policy_reason": "<non-empty human-readable reason>" }
}
```

`provenance.required: false` additionally requires a non-empty `reason` and
exempts the workload from `.github/scripts/validate-oxidebatch-provenance.py`'s
requirement that a workload manifest declare a first-party OxideBatch
validation subject. It exists solely for the bounded, non-product
orchestration fixture (`fixture-heterogeneous/`) described below — a real
validation workload must always declare `provenance.required: true`, and
`validate-oxidebatch-provenance.py` enforces exact published-artifact
provenance for every such workload exactly as it did before this contract
existed.

`.github/scripts/validate-workload-registry.py` fails closed if a registered
workload is missing `ci/validate`, the file exists but is not executable, or
`msrv`/`provenance` are missing or malformed.

## Why a fixture workload exists

`fixture-heterogeneous/` is not a validation workload and makes no OxideBatch
evidence claim. Its only purpose is to prove, in real CI, that the central
workflow's fan-out is contract-driven: it has no PostgreSQL, no services, a
materially different `ci/validate` implementation, `msrv.declared: false`,
and `provenance.required: false`, and it participates in the exact same
fan-out/aggregate machinery as `csv-postgres` with zero workload-name
branching in the central workflow.
