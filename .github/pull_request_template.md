## Summary

<!-- What workload, validation contract, evidence, CI, or repository policy changes, and why? -->

## Related issue / decision

<!-- Link the owning campaign/governance issue and any governing decision. -->

## Dependency provenance

<!-- Exact OxideBatch version and source. Confirm no path/git/local patch dependency, or write N/A for repository-only changes. -->

## Correctness and restart impact

<!-- Transactions, checkpoints, process crash/restart, input identity, idempotency, ordering, verification. -->

## Resource and performance impact

<!-- Memory, connections, file handles, buffers, runtime observations, benchmark implications. -->

## Verification and evidence

<!-- Exact commands/tests/manual experiments run and evidence regenerated. State skipped checks and why. -->

## Framework findings

<!-- Link OxideBatch issues for bugs/API/docs/semantics/performance findings, or write None. -->

## Security and diagnostics

<!-- Secrets/PII/redaction, error provenance, Actions permissions, supply-chain impact. -->

## Scope / deferred work

<!-- Explicitly identify anything intentionally out of scope. -->

## Checklist

- [ ] The workload still consumes the intended exact published OxideBatch artifact from the public registry, or this is N/A for repository-only work.
- [ ] `Cargo.lock` provenance is correct and reproducible where applicable.
- [ ] Tests cover the semantic risk introduced by this change.
- [ ] Restart claims use a real new-process restart where required.
- [ ] Verification independently detects incorrect final state where a business-state claim is made.
- [ ] No framework defect is hidden by an undocumented consumer workaround.
- [ ] Evidence/documentation were regenerated or confirmed applicable.
- [ ] Logs/evidence contain no secrets, real personal data, or sensitive payloads.
- [ ] Relevant locked Cargo checks and real integration tests were run or explicitly recorded as not applicable.
- [ ] Exact final PR HEAD was reviewed; any HEAD movement invalidates prior final evidence.
- [ ] Required CI is green for that exact final HEAD.
