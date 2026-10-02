# Hardening Reassessment — 2026-09-20

- **Status:** Completed point-in-time reassessment
- **Owning issue:** #104 — completed 2026-09-22
- **Rust CodeQL residual:** #123 — completed 2026-09-22

This document records the September hardening reassessment and its closure. It
is historical evidence, not a live backlog. Current repository policy lives in
`repository-settings-policy.json`, the live ruleset, and the active workflow
contracts.

The reassessment re-evaluated the completed repository hardening against
current external GitHub/OpenSSF guidance and the repository's then-current
code/language surfaces. Existing aggregate gates, dependency review,
supply-chain/evidence contracts, label taxonomy, trusted-base metadata
automation, and recurring drift architecture remain authoritative.

## GAP — backlog mutation default

`label-automation.yml` already has a strong reconciliation model and a safe trusted-base `pull_request_target` boundary. The problem was its manual backfill default: `dry_run=false` made mutation the default operator action.

Backfill now defaults to dry-run. A human must opt into mutation after reviewing proposed changes.

The privileged trigger is intentionally retained. It checks out only `github.event.repository.default_branch`, disables checkout credential persistence, and executes repository-owned reconciliation code; it never executes pull-request-head code. This preserves fork metadata without weakening the trust boundary.

## GAP — workflow semantic/security scanners

The repository-owned workflow validator already checked immutable action refs and selected privileged boundaries, but that does not replace a real Actions parser or independent vulnerability-pattern scanner.

Required CI now executes:

- checksum-pinned actionlint 1.7.12;
- checksum-pinned zizmor 1.30.0 with online audits disabled;
- all checked-in workflow files;
- an adversarial negative workflow that both tools must reject for the expected reason.

`validate-workflow-security.py` also fails closed if scanner wiring/checksum structure disappears, and the hardening-drift fixture suite proves those policy failures are detectable.

## GAP — CodeQL language coverage drift

The last admin readback on 2026-09-03 proved GitHub default setup was configured only for `actions` and `python`.

That is no longer sufficient desired coverage because the repository contains first-class Rust workload implementations in addition to its GitHub Actions and Python governance tooling.

The desired single default-setup authority is therefore:

- `actions`
- `python`
- `java-kotlin`
- `rust`

Java/Kotlin is now evidence-backed rather than speculative. On exact PR #121 head
`5d38a31a6247c76edfa5490aa6f5cbbf0a5066b3`, GitHub-managed default setup ran
`Analyze (java-kotlin)` with build mode `none`, discovered the nested Maven
benchmark modules, and reported that it scanned all 5 maintained Java files.
That makes Java/Kotlin useful advisory security coverage for the maintained
comparative harnesses; it is not benchmark-correctness authority.

Rust was the final residual at the time of this reassessment. That residual is
now closed. Issue #123 retained the supported default-setup remediation and
GitHub-managed run `35684911979` on exact
`main@93139667564c87cdcb3d1e0a8e930ffee85e4fdd` completed successfully with
`Analyze (actions)`, `Analyze (python)`, `Analyze (java-kotlin)`, and
`Analyze (rust)`; the Rust analysis and SARIF/results upload steps also
succeeded. No competing checked-in advanced-setup workflow was introduced.

Current `main@c2779aff0b5917b9b40c9bb812ffd334423484ef` continues to emit the
same four successful managed analyses in run `36115060879`. GitHub default
setup remains the single CodeQL authority, and the administrative language
selection remains a manual-readback control.

## PASS — existing dependency and repository governance

Dependency Review is already a required protected-main context and the public Dependency Graph is live. The existing scheduled hardening audit already distinguishes policy drift from infrastructure/readback failure and keeps admin-gated controls explicit as manual readback.

No second taxonomy, ruleset, or release authority is introduced.

## Closure evidence

The reassessment closed only after the repository and live producer evidence
satisfied the owned gaps:

- label backfill defaults to dry-run and preserves the audited trusted-base
  mutation boundary;
- checksum-pinned actionlint and zizmor plus the adversarial negative control
  are exercised by the required validation path and recurring hardening audit;
- Java/Kotlin default-setup coverage was measured over all 5 maintained Java
  benchmark files;
- #123 closed the Rust residual with managed `Analyze (rust)` and successful
  analysis/SARIF/results upload;
- #104 recorded successful current-main hardening/policy evidence before
  completion;
- no second CodeQL authority, taxonomy, ruleset, or release authority was
  introduced.

A later machine-enforced failure declaration/classification protocol was subsequently retired. Current machine policy/live ruleset remains authoritative without retroactively rewriting this dated assessment.

`UNKNOWN`, `UNVERIFIED`, and `INSUFFICIENT EVIDENCE` remain FAIL for claimed controls.
