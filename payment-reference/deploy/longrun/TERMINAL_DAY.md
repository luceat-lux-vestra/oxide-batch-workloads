# Terminal-day orchestration (#318)

`campaign_day_terminal.py` is the minimal per-day sequencing layer for the
payment-reference long-run campaign. It is deliberately **not** an acceptance
authority.

## Authority boundary

The controller may execute or invoke existing producers in order, but it cannot
declare a day PASS from their exit codes. Terminal success is materialized only
by `campaign-evidence.py::materialize_terminal_day()`, which:

1. assembles descriptors for all eight retained proof families;
2. independently re-runs the canonical day-verification checks and cross-bindings;
3. retains the canonical `NNNNNN.verification.json`;
4. retains the terminal `NNNNNN.json` bound to that verification digest;
5. appends `day-terminal-success`;
6. re-runs the read-only frontier scanner and requires the frontier to advance.

The eight proof families remain:

- settlement roster;
- settlement close;
- provider side-effect cardinality;
- recovery;
- reconciliation;
- financial invariants;
- canonical online execution;
- quiescence.

Controller sequencing records are never financial truth.

## Sequence

The current terminal sequence is source-driven by the producer contracts:

1. canonical normal+fault online execution;
2. independent canonical-online proof;
3. recovery proof over already-converged recovery state;
4. reconciliation proof over already-completed reconciliation state;
5. settlement roster after day outcomes have converged;
6. provider statement + Merchant settlement close proof;
7. day-wide provider side-effect cardinality;
8. cross-system financial invariants;
9. per-day quiescence sampling after all mutation stages;
10. independent quiescence proof;
11. canonical eight-proof terminal materialization and frontier re-scan.

Recovery/reconciliation workload execution and scheduled hard-crash injection
remain separate qualified execution concerns. If their required state/evidence is
not present, their proof producer blocks the terminal controller. Re-running the
controller resumes the same active day and reuses create-only proof artifacts
where the underlying producer contract permits exact replay.

## Crash safety

`day-started` is appended once. A per-campaign terminal-controller lock serializes
controller invocations, and the controller may resume an active day without
adding another start event. Terminal verification/day files use exact-content
create-only retention; a crash after retaining the verification file can resume
by proving the exact same candidate and completing the remaining terminal
materialization.

A completed day is a no-op on controller replay; payment and settlement stages
are not re-executed after the frontier has already advanced.

## Runtime configuration

Environment-dependent endpoints, paths, binary locations and timeouts are
explicit controller parameters. The controller derives campaign ID, frozen
source revision, provenance directory and deterministic artifact names from the
initialized campaign itself.

The Compose file/env selection is also propagated explicitly to subprocesses
that still consume the existing `PAYMENT_REFERENCE_COMPOSE_FILE` /
`PAYMENT_REFERENCE_COMPOSE_ENV_FILE` interface.

See `python3 payment-reference/deploy/longrun/campaign_day_terminal.py --help`
for the exact runtime parameters.

## Out of scope

This layer does **not** implement campaign-wide acceptance. The following remain
separate final-campaign boundaries:

- >=24 hours elapsed;
- >=30 completed logical days;
- >=3000 primary payment flows;
- final >=1800-second quiescence.

It also does not turn controller events, subprocess exit codes or generic
`PASS + SHA` records into acceptance authority.
