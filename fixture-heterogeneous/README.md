# fixture-heterogeneous

**Not a validation workload.** This is a bounded, non-product CI-orchestration
fixture. It exists only to prove, in real CI, that
`.github/workflows/ci.yml`'s fan-out and aggregate machinery is driven by the
[`ci/validate` workload contract](../.github/WORKLOAD_CONTRACT.md) and
`workloads.json` registry data, and contains no `csv-postgres`-specific or
workload-name-specific branching.

It deliberately differs from `csv-postgres` in every way the contract allows
a workload to differ:

- no OxideBatch dependency at all -- registered under `workloads.json`'s
  `fixtures` array, not `workloads`, which is what structurally (not just
  declaratively) keeps `validate-oxidebatch-provenance.py` (#29) from ever
  looking at this project;
- no database, no services, no migration;
- no declared MSRV (`msrv.declared: false`, with an explicit `policy_reason`
  instead of a silently skipped shard);
- a completely different golden-path smoke mechanism: a deterministic
  word-histogram over a checked-in text fixture, diffed against a checked-in
  golden JSON file, instead of a database round-trip.

Do not add real product/business logic here, and do not use this as a
template for a second real validation workload — see
[`ROADMAP.md`](../ROADMAP.md) and the workload issue template for how a real
workload gets proposed.

## Running locally

```sh
cargo test
./ci/validate ci
```
