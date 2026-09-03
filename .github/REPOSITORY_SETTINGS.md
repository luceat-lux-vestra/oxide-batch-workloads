# Repository settings hardening

This document records the expected live repository controls for issue #37 and
defines which controls can be audited automatically by #38 without adding a
high-privilege credential. The machine-readable source of truth is
[`repository-settings-policy.json`](repository-settings-policy.json).

## Control semantics

Controls are classified by enforcement value, not by visual parity with
another repository:

- `required`: the live value is part of the accepted hardening posture;
- `conditional`: the value is intentional but becomes a stronger requirement
  only when its prerequisite governance/compatibility decision is made;
- `advisory/hygiene`: useful repository hygiene, not a security boundary;
- `manual-readback`: reserved for controls that cannot be safely or
  deterministically audited with the credentials/API surface available to the
  repository automation.

The `readback` field is separate from classification. `repository-api` and
`ruleset-api` controls are suitable for low-privilege drift auditing. Controls
marked `manual-readback` must not cause #38 to introduce a high-privilege PAT
solely for convenience.

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
- stable required contexts `dependency-review`, `supply-chain`,
  `workloads-ci`, and `workloads-msrv`;
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

The machine policy still classifies CodeQL as `manual-readback` for future
#38 drift auditing because the connected low-privilege surface does not expose
the administrative default-setup setting itself. A recent dynamic run is
useful acceptance evidence, but absence of a recent run is not a reliable
proof that the setting was disabled. #37 additionally confirmed the admin
`code-scanning/default-setup` state directly (see the readback table above):
`state=configured`, languages `actions` and `python`, default query suite,
`remote` threat model, weekly schedule.

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
matched the accepted policy — no live settings changes were required. This
is distinct from what #38 can safely do: #38 must not add an
administration-scoped PAT, so these controls remain classified
`manual-readback` in the machine policy even though a human/agent session
was able to read them here.

| Control | Live value (confirmed 2026-09-03) | Evidence |
| --- | --- | --- |
| Actions default workflow permissions | `read`, cannot approve PRs | `gh api repos/.../actions/permissions/workflow` |
| Actions/reusable-workflow policy | `allowed_actions=all`, `sha_pinning_required=false` | `gh api repos/.../actions/permissions` |
| Fork pull-request contributor approval | `first_time_contributors` | `gh api repos/.../actions/permissions/fork-pr-contributor-approval` |
| Dependency Graph | Active (235-package SBOM returned) | `gh api repos/.../dependency-graph/sbom` |
| Dependabot alerts | Enabled (HTTP 204) | `gh api repos/.../vulnerability-alerts` |
| Dependabot security updates | Enabled | `security_and_analysis.dependabot_security_updates.status` |
| Secret scanning | Enabled | `security_and_analysis.secret_scanning.status` |
| Secret scanning push protection | Enabled | `security_and_analysis.secret_scanning_push_protection.status` |
| Private Vulnerability Reporting | Enabled | `gh api repos/.../private-vulnerability-reporting` |
| CodeQL default setup | `configured`, languages `actions`+`python`, weekly schedule | `gh api repos/.../code-scanning/default-setup` |

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

## #38 boundary

#38 should consume the canonical JSON policy rather than re-encode settings.
It may automatically compare controls marked `repository-api` or
`ruleset-api` using the ordinary repository credential. It should report or
track drift without mutating settings automatically.

The first #38 implementation must not add an administration-scoped PAT solely
to convert the manual controls above into automated checks. If GitHub later
exposes a sufficiently low-privilege read path, the policy can change those
controls from `manual-readback` to the corresponding machine readback mode.
