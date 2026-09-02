# Contributing

Thank you for improving the OxideBatch real-workload validation suite.

This repository is not the OxideBatch framework source tree. It validates **published** framework artifacts as an independent consumer. Framework defects found here should be reproduced and reported in `luceat-lux-vestra/oxide-batch`; do not patch around them with local framework source.

## Before opening a change

Read `AGENTS.md`, the repository `README.md`, and the README/evidence for the workload you are changing.

Keep a pull request focused on one validation concern. Large new workloads or benchmark campaigns should start with an issue describing the contract being validated, why an existing workload cannot cover it, and the evidence required for completion.

## Dependency provenance

A workload must pin the OxideBatch release it validates with an exact version and commit its lockfile. OxideBatch must resolve from the public registry. Path, git, workspace, local patch, or modified vendored framework dependencies are not acceptable validation inputs.

Dependency upgrades that change the OxideBatch version are semantic validation changes, not routine dependency maintenance. They require regenerated/reviewed evidence and updated documentation.

## Validation expectations

Use deterministic datasets and failure injection. Stateful/restartable workloads must test durable state and final business state. A claimed restart must use a new process after a real process failure where that distinction matters.

Run the narrow checks needed while iterating, then the relevant full workload gate before requesting review. Each workload owns its authoritative CI behind `<workload>/ci/validate` (see `.github/WORKLOAD_CONTRACT.md`); for `csv-postgres` that is formatting, Clippy with warnings denied, locked builds, a production unwrap/expect guard, real PostgreSQL integration tests, a golden-path smoke test, and the declared MSRV build. Run `./ci/validate ci` (and `./ci/validate msrv` where applicable) from the workload's own directory to reproduce exactly what CI runs.

Do not claim tests or manual validation that were not actually run. Document skipped checks and the reason.

## Evidence

Generated evidence must be reproducible and must identify the dependency, dataset, environment, and result sufficiently to interpret it later. Do not hand-edit generated evidence to make it agree with an expected outcome.

When a change invalidates existing evidence, regenerate it or explain precisely why it remains applicable.

## Framework findings

Search the OxideBatch issue tracker before filing a new framework issue. Classify findings narrowly as bug, API ergonomics, missing capability, documentation gap, performance issue, or semantics/contract ambiguity. Link the issue from the workload documentation.

## Pull requests

Use the pull request template. Explain:

- what contract changed or was validated;
- exact OxideBatch version/source affected;
- correctness/restart/resource impact;
- automated and manual evidence;
- framework findings and linked issues;
- any limitations or intentionally deferred work.

CI green is necessary but not sufficient. Merge approval applies only to the exact reviewed HEAD.