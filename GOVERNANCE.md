# Repository Governance

This repository uses GitHub-native planning metadata as the authoritative source for ownership and hard blockers.

## Native issue hierarchy

The authoritative capability/evidence ownership hierarchy is:

```text
Epic -> Track -> Campaign
```

- The program Epic owns Track issues through native GitHub Sub-issues.
- Each concrete Campaign has exactly one primary Track parent through native GitHub Sub-issues.
- A Campaign may contribute evidence to other Tracks, but those secondary relationships remain prose cross-links unless one of them is separately proven to be the Campaign's primary ownership boundary.
- `Parent track:` text, issue-body navigation, roadmap references, and cross-links are explanatory. They must agree with native metadata and are not a substitute for the native hierarchy.
- A closed Campaign remains attached to its owning Track; completion does not erase ownership history.

## Native issue dependencies

Native GitHub Issue Dependencies represent only true hard blockers: work that cannot validly begin or complete until the prerequisite is satisfied.

Do not encode any of the following as hard dependencies merely for planning convenience:

- soft sequencing or preferred execution order;
- evidence cross-links;
- milestone co-membership;
- redundant transitive blockers;
- already-completed prerequisites that no longer represent a live blocker.

If a hard dependency is not proven, leave it unencoded rather than guessing.

## Milestones

Native milestones are a separate governance axis from capability ownership. They group concrete Campaigns under a bounded published-release evidence horizon.

Milestone membership does not replace Epic -> Track -> Campaign ownership and does not authorize speculative work. Long-lived Epic and Track issues remain milestone-null unless an explicit governance decision changes that model.

Do not invent due dates. A due date must have an externally justified deadline or an explicit planning commitment.

## Audit invariant

Before campaign activation, governance mutation, or release-horizon closure, fresh-read GitHub metadata from the current authoritative `main` state.

The audit fails closed when any of the following is missing, stale, contradictory, or otherwise unverified:

- expected native parent/child ownership;
- required true hard dependencies;
- agreement between native metadata and prose ownership/cross-links;
- milestone assignment and release-family semantics;
- required post-merge validation state.

`UNKNOWN`, `UNVERIFIED`, and `INSUFFICIENT EVIDENCE` are failures, not reasons to assume the intended state.

## Merge and closure gate

Repository governance changes that require a pull request follow the same proof-obligation gate as product work:

- review the exact final PR HEAD;
- any HEAD movement invalidates the prior review verdict;
- GitHub Actions is the authoritative final verification source;
- CI green is necessary but not sufficient;
- squash merge only, guarded by the exact expected HEAD SHA;
- returned squash SHA must equal a fresh read of `main`;
- close the governing issue only after successful post-merge validation.
