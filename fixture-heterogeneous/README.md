# fixture-heterogeneous

Non-production fixture workload used to prove repository CI orchestration is
contract-driven and can execute a registered workload whose validation shape is
not PostgreSQL-specific.

- Depends on published `oxide-batch = "=0.6.0"` from crates.io.
- Owns a workload CI contract at `ci/validate.sh`.
- Explicitly declares no MSRV gate (`not-applicable`) in `workloads.json`.

Its validation contract intentionally runs only local compile/lint/test/smoke
checks and requires no database service bootstrap.
