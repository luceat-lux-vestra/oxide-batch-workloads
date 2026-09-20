# Hardening Reassessment — 2026-09-20

Owning issue: #104

This pass re-evaluates the completed repository hardening against current external GitHub/OpenSSF guidance and the repository's present code/language surfaces. Existing aggregate gates, dependency review, supply-chain/evidence contracts, label taxonomy, trusted-base metadata automation, and recurring drift architecture remain authoritative.

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

That is no longer sufficient desired coverage. The repository contains:

- GitHub Actions workflows;
- Python governance/validation tooling;
- first-class Rust workload implementations;
- maintained Java benchmark controls.

GitHub's current CodeQL product documentation supports all four surfaces. The desired single default-setup authority is therefore:

- `actions`
- `python`
- `rust`
- `java-kotlin`

There is an important current API/documentation mismatch: GitHub's compiled-language guidance documents Rust default-setup analysis with build mode `none`, while the REST `default-setup` update endpoint's published `languages` enum does not currently list `rust`. This PR therefore records desired state without inventing an undocumented Rust API mutation. The exit proof must use authoritative admin readback after the supported GitHub UI/default-setup path has re-detected or enabled Rust; Java/Kotlin may be configured through the documented API where appropriate.

## PASS — existing dependency and repository governance

Dependency Review is already a required protected-main context and the public Dependency Graph is live. The existing scheduled hardening audit already distinguishes policy drift from infrastructure/readback failure and keeps admin-gated controls explicit as manual readback.

No second taxonomy, ruleset, or release authority is introduced.

## Exit criteria

- exact final PR HEAD passes current required contexts;
- actionlint and zizmor pass the real workflows and reject the negative fixture;
- static hardening tests reject scanner removal/checksum weakening;
- authoritative CodeQL readback proves all four desired language surfaces; Rust is not claimed from an undocumented REST mutation;
- merged-main required checks and recurring hardening policy remain green;
- label backfill mutation is exercised only after dry-run review.

`UNKNOWN`, `UNVERIFIED`, and `INSUFFICIENT EVIDENCE` remain FAIL for claimed controls.
