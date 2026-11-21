# Day reconciliation verification contract v1 (#318)

This contract replaces the legacy terminal-day `reconciliation: PASS + SHA`
placeholder with a retained, independently replayable proof over the exact
`payment-reconciliation-postgres` 0.7.0 semantics.

## Retained artifact

The day proof is retained at:

`days/day-NNNNNN.reconciliation.verification.json`

with schema:

`oxide-batch-workloads.payment-reference-day-reconciliation-verification/v1`

and decision:

`RECONCILIATION_VERIFIED_NOT_DAY_ACCEPTED`.

The artifact binds:

- campaign ID and logical day;
- exact campaign source revision;
- exact deterministic day-plan digest;
- OxideBatch version `0.7.0`;
- exact `payment-reconciliation-postgres` binary SHA-256;
- SHA-256 of the retained independent verifier source;
- the complete secret-free workload producer report;
- the logical-day reconciliation crash schedule and, when scheduled, retained
  crash execution evidence.

## Snapshot identity

The proof accepts only the root live CLEAR snapshot for the logical day.

Required metadata:

- snapshot ID `<campaign-id>:day-NNNNNN:reconciliation`;
- contract `payment-reconciliation-v1` through the workload-owned snapshot;
- logical business date equal to the campaign day;
- provider scope equal to the deterministic Reference Merchant identity;
- scenario case `CLEAR`;
- no base snapshot;
- policy `payment-reconciliation-policy-v1`;
- empty mutation recipe.

Derived test scenarios are qualification evidence, not campaign-day evidence.

## Independent replay

The verifier dynamically reuses the retained v0.7.0 reconciliation verifier
semantics from
`payment-reconciliation-postgres/evidence/verify-retained-evidence.py`.

It independently recomputes:

- internal snapshot digest;
- provider snapshot digest;
- the seven-way reconciliation detail classification;
- mismatch count;
- aggregate financial delta;
- result digest.

Campaign acceptance requires:

- workload processing state `PROCESSED`;
- business state `CLEAR`;
- mismatch count `0`;
- aggregate delta `0`;
- exact detail rows equal to the independent oracle;
- exactly one published CLEAR reconciliation row.

The retained workload-reported `violations` array must be empty, but that field
alone is never trusted as proof.

## Day-cardinality cross-binding

The day frontier independently compares the reconciliation provider snapshot
against the separately produced 100-slot end-of-day provider-cardinality proof.

The reconciliation snapshot must contain exactly the stable v1 provider
membership represented by EOD statuses:

- `authorized`;
- `captured`;
- `refunded`.

For every retained provider row, the frontier requires exact agreement on:

- provider payment identity;
- normalized provider status;
- authorized amount;
- captured amount;
- refunded amount;
- provider version.

Cancelled and failed provider rows are intentionally outside the reconciliation
workload's frozen v1 Merchant-state scope and therefore are not members of this
snapshot.

## Scheduled crash evidence

Only the already-qualified reconciliation crash schedule is accepted:

- day mod 10 == 3: `during-write` + public recovery/run/finalize;
- day mod 10 == 6: `after-business-commit` + public recovery/run/finalize;
- day mod 10 == 0: finalize `after-commit` + finalize replay.

A scheduled crash requires:

- observed semantic marker;
- real SIGKILL;
- killed process proven gone;
- continuation in a different process;
- correct recovery/replay path for the boundary;
- terminal `PROCESSED` state;
- exactly one final CLEAR publication.

Crash evidence is forbidden on other logical days.

## Claim boundary

This proof validates retained reconciliation state. It does not itself execute
the workload, mutate Merchant/provider state, start the OCI campaign, or advance
a logical-day frontier.

The live campaign producer/controller that captures this retained artifact is a
separate implementation slice.
