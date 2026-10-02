# Repository settings hardening

This document records the expected live repository controls for issue #37 and
defines which controls can be audited automatically by #38 without adding a
high-privilege credential. The machine-readable source of truth is
[`repository-settings-policy.json`](repository-settings-policy.json).

## Control semantics

Every control has two independent fields, and the two vocabularies must not
be conflated:

- `classification` says how binding the value is, by enforcement weight, not
  by visual parity with another repository: `required` (the live value is
  part of the accepted hardening posture), `conditional` (intentional today,
  becomes a stronger requirement only once its prerequisite
  governance/compatibility decision is made), or `advisory/hygiene` (useful
  repository hygiene, not a security boundary).
- `readback` says how the live value can be verified: `repository-api` or
  `ruleset-api` for controls a low-privilege, repo-scoped credential (or, for
  a handful of endpoints, no credential at all) can read deterministically;
  `manual-readback` for controls that cannot be safely or deterministically
  audited with the credentials/API surface available to ordinary repository
  automation. `manual-readback` is a `readback` value only — it is never a
  valid `classification`. Controls marked `manual-readback` must not cause
  #38 to introduce a high-privilege PAT solely to convert them into
  automated checks.

## Live API readback established for #37

The repository API readback after #35 confirmed:

- repository is public and the default branch is `main`;
- squash merge is enabled; merge commits and rebase merge are disabled;
- automatic merge and branch-update support are enabled;
- merged head branches are deleted automatically;
- squash title policy is `COMMIT_OR_PR_TITLE` and squash message policy is
  `COMMIT_MESSAGES`;
- web commit signoff is not required. This setting is not equivalent to
  cryptographic signed-commit enforcement.

The active `Protect main` ruleset (id `21944159`) readback confirmed:

- active enforcement on the default branch with no bypass actors;
- deletion and non-fast-forward protection;
- linear history;
- pull requests required and squash as the only allowed merge method;
- review-thread resolution;
- strict required status checks;
- stable required contexts `dependency-review`, `supply-chain`, `workloads-ci`,
  and `workloads-msrv`;
- `required_approving_review_count=0` with no code-owner or last-push approval
  requirement;
- `require_extra_approval_for_unattributed_changes=false`.

The zero-approval policy is intentional for the current process: the
repository's authoritative merge control is an exact-final-HEAD strict gate.
`require_extra_approval_for_unattributed_changes` is therefore not counted as
an effective approval control. Enabling mandatory human approval would be a
separate governance decision.

There is no `required_signatures` rule in the accepted ruleset. Signed commits
remain conditional until compatibility with Dependabot and other repository
automation is proven rather than assumed.

## Code scanning readback

GitHub generated a `dynamic/github-code-scanning/codeql` workflow run for PR
#56. Its live jobs initialized and performed CodeQL analysis for both `python`
and `actions`. This is direct runtime evidence that GitHub CodeQL default setup
is enabled for this repository even though there is intentionally no checked-in
CodeQL workflow file.

The machine policy classifies CodeQL and its selected language set as
`manual-readback` for future drift auditing because the connected
low-privilege surface does not expose the administrative default-setup setting
itself. A recent dynamic run is
useful acceptance evidence, but absence of a recent run is not a reliable
proof that the setting was disabled. #37 confirmed the admin
`code-scanning/default-setup` state as `configured` with `actions` and
`python`; that 2026-09-03 language list is now stale evidence rather than an
accepted coverage claim because the repository contains maintained Rust
workloads and Java benchmark controls.

Hardening Reassessment #104 now declares desired single-authority
default-setup coverage as `actions`, `python`, `java-kotlin`, and `rust`.

Java/Kotlin is no longer a speculative coverage target. On exact PR #121 head
`5d38a31a6247c76edfa5490aa6f5cbbf0a5066b3`, GitHub-managed default setup
successfully ran `Analyze (java-kotlin)` in build mode `none`, discovered
the nested Maven benchmark modules (`raw-jdbc`, `spring-batch`, and
`jberet`), and reported that CodeQL scanned 5 of 5 Java files. This satisfies
the measurement side of #120 and makes Java/Kotlin useful advisory security
coverage for the maintained comparison harnesses. It remains non-authoritative
for benchmark correctness.

That residual is now closed. Hardening Reassessment #104 / #123 retained
the supported default-setup remediation and GitHub-managed run
`35684911979` on exact `main@93139667564c87cdcb3d1e0a8e930ffee85e4fdd`
successfully emitted `Analyze (actions)`, `Analyze (python)`,
`Analyze (java-kotlin)`, and `Analyze (rust)`; the Rust analysis, SARIF
artifact upload, and final results upload all succeeded. No competing
advanced-setup workflow was introduced.

Current `main@b90690210c97a29310f9f071d6663fa7f9b14200` continues to emit the
same four successful managed analyses in CodeQL run `36952216929`.
The configured language list remains an admin/manual-readback control; managed
producer success is runtime evidence, not a reason to weaken that readback
classification or add a second CodeQL authority.

## Workflow-level token posture

Repository workflows reviewed during #37 explicitly request narrow token
permissions. The main CI, dependency review, label policy/taxonomy, and
scheduled policy workflows use read-only contents permission where possible;
write permissions are declared only by workflows that actually mutate issues
or pull requests. Checkout steps in protected CI use
`persist-credentials: false`.

This is defense in depth, but it is **not** a substitute for reading the live
repository-wide Actions default `GITHUB_TOKEN` setting. That live admin setting
remains a manual readback item below.

## Actions and security administration readback (#37)

An admin-scoped `gh api` credential was used once, directly by the #37
agent, to perform the readback below and to confirm every value already
matched the accepted policy — no live settings changes were required. That
credential is **not** available to #38, so each row is labeled with the
`readback` mode it actually keeps in the machine policy:

- **admin-gated (`manual-readback`)** — verified by re-issuing the same
  request with no `Authorization` header at all: it returns HTTP 401
  ("Requires authentication"). A low-privilege, repo-scoped `GITHUB_TOKEN`
  cannot be assumed to pass where an unauthenticated request fails, so #38
  must not treat these as automatable without further verification, and
  must not add an administration-scoped PAT to convert them.
- **publicly readable (`repository-api`)** — verified by re-issuing the same
  request with no `Authorization` header: it returns HTTP 200 with the same
  body. These do not need any elevated credential and are safe for #38 to
  poll with an ordinary token or none at all.

| Control | Live value (confirmed 2026-09-03) | Evidence | Readback mode |
| --- | --- | --- | --- |
| Actions default workflow permissions | `read`, cannot approve PRs | `gh api repos/.../actions/permissions/workflow` | admin-gated |
| Actions/reusable-workflow policy | `allowed_actions=all`, `sha_pinning_required=false` | `gh api repos/.../actions/permissions` | admin-gated |
| Fork pull-request contributor approval | `first_time_contributors` | `gh api repos/.../actions/permissions/fork-pr-contributor-approval` | admin-gated |
| Dependency Graph | Active (235-package SBOM returned) | `gh api repos/.../dependency-graph/sbom` | **publicly readable** |

Dependency Graph being active is a live repository setting, not a consequence of public visibility. GitHub allows repository administrators to enable or disable it; the populated SBOM is therefore retained as direct operational evidence and as a drift signal.
| Dependabot alerts | Enabled (HTTP 204) | `gh api repos/.../vulnerability-alerts` | admin-gated |
| Dependabot security updates | Enabled | `security_and_analysis.dependabot_security_updates.status` | admin-gated |
| Secret scanning | Enabled | `security_and_analysis.secret_scanning.status` | admin-gated |
| Secret scanning push protection | Enabled | `security_and_analysis.secret_scanning_push_protection.status` | admin-gated |
| Private Vulnerability Reporting | Enabled | `gh api repos/.../private-vulnerability-reporting` | **publicly readable** |
| CodeQL default setup | `configured`, languages `actions`+`python`, weekly schedule | `gh api repos/.../code-scanning/default-setup` | admin-gated |

`security.dependency_graph` and `security.private_vulnerability_reporting`
are therefore classified `readback: repository-api` in the machine policy,
not `manual-readback` — #38 can poll them with an ordinary low-privilege
token (or no token). Every other row above stays `manual-readback`.
`vulnerability-alerts`, `security_and_analysis` (returned inside the repo
GET response only for an authenticated caller with push access or higher —
confirmed absent from an unauthenticated GET), `actions/permissions/*`, and
`code-scanning/default-setup` all returned HTTP 401 when re-issued without
authentication, consistent with requiring elevated access.

The `allowed_actions=all` policy was intentionally left unrestricted rather
than narrowed to a selected-actions allowlist: CI depends on several
third-party actions and a compatibility pass for a narrower allowlist was
not performed during #37. The binding fork-PR protection is not this
allowlist; it is GitHub's default read-only, secret-less `GITHUB_TOKEN` for
`pull_request`-triggered runs, combined with the contributor-approval gate
above. This repository has exactly one `pull_request_target` consumer,
`label-automation.yml`; it always checks out the trusted default branch and
never PR-head code (see the comment at the top of its `reconcile` job), so
it does not combine a privileged token with untrusted code.

`SECURITY.md` tells reporters to use GitHub Private Vulnerability Reporting
for issues in scope of this repository and to route OxideBatch framework
vulnerabilities to the framework repository's Security Advisories. This now
reflects the confirmed-enabled PVR state above rather than a conditional
"when enabled" hedge.

## Actions event-policy readback after failure-triage retirement

Issue #126 established two path-scoped repository Actions event policies before
GitHub's public-repository `pull_request_target` default enforcement:

- policy `5160`: `.github/workflows/failure-triage.yml` ->
  `pull_request_target`;
- policy `5161`: `.github/workflows/label-automation.yml` ->
  `issues`, `pull_request_target`, `workflow_dispatch`.

PR #147 subsequently retired `failure-triage.yml` and the machine-enforced
failure-declaration protocol. The accepted live settings state is therefore now:

- policy `5161` remains active for the audited, trusted-base-only label
  automation workflow;
- policy `5160` is absent. A path-scoped exception must not outlive the
  workflow and governance contract that justified it.

Repository Actions policy resources are independent of workflow files and their
REST read/update/delete surface requires repository Administration authority.
The ordinary hardening audit intentionally does not carry that credential.
Both expectations are therefore explicit `manual-readback` controls in
`repository-settings-policy.json`, and the policy contract test fails if
either expectation disappears from the source of truth.

The 2026-10-03 final hardening sweep could not authoritatively read or delete
policy `5160` through the connected low-privilege repository integration.
Until an administration-scoped readback confirms `5160` absent (deleting it
if still present) and confirms `5161` still matches its exact path/event
contract, the checked-in policy is reconciled but that live administration
surface remains an explicit manual-readback obligation.

## #38 boundary

#38 should consume the canonical JSON policy rather than re-encode settings.
It may automatically compare controls marked `repository-api` or
`ruleset-api` using the ordinary repository credential. It should report or
track drift without mutating settings automatically.

The first #38 implementation must not add an administration-scoped PAT solely
to convert the manual controls above into automated checks. If GitHub later
exposes a sufficiently low-privilege read path, the policy can change those
controls from `manual-readback` to the corresponding machine readback mode.
