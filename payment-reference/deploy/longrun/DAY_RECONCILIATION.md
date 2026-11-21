# Day reconciliation verification contract v2 (#318)

This contract replaces the legacy terminal-day `reconciliation: PASS + SHA`
placeholder with a retained, independently replayable proof over the exact
`payment-reconciliation-postgres` 0.7.0 semantics.

## Retained artifact

The day proof is retained at:

`days/day-NNNNNN.reconciliation.verification.json`

with schema:

`oxide-batch-workloads.payment-reference-day-reconciliation-verification/v2`

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

## Version boundary

Reconciliation proof schema version 1 is intentionally superseded before live
campaign execution. Its snapshot identifier used ':' separators, while the
published `payment-reconciliation-postgres` workload rejects ':' in snapshot IDs.
Version 2 freezes the workload-compatible identifier below; v1 retained files are
therefore rejected by the current day-verification scanner.

## Snapshot identity

The proof accepts only the root live CLEAR snapshot for the logical day.

Required metadata:

- snapshot ID `<campaign-id>-day-NNNNNN-reconciliation` (the workload's ASCII-safe snapshot identifier contract forbids `:`);
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

## Read-only producer CLI

After the reconciliation workload has already reached its final state, retain the
day proof with the exact campaign-pinned binary:

```bash
export PAYMENT_RECONCILIATION_DATABASE_URL='<existing workload database URL>'

python3 payment-reference/deploy/longrun/campaign_day_reconciliation.py \
  --campaign-dir '<campaign evidence directory>' \
  --day N \
  --workload-binary '<exact payment-reconciliation-postgres binary>' \
  [--crash-evidence '<qualified scheduled-crash evidence JSON>']
```

The producer verifies the binary SHA-256 against `campaign.json`, invokes only:

`DATABASE_URL=... payment-reconciliation-postgres verify --snapshot-id ...`

and then runs this module's independent validator before create-only retention.
The database URL is passed through the child environment rather than process argv.
An identical existing output is an idempotent replay; differing existing bytes
are a hard conflict. Database URLs and command stderr are not serialized into
the retained proof.

## Claim boundary

This proof validates and retains already-completed reconciliation state. The
producer does not create a source snapshot, execute or recover the OxideBatch
job, finalize publication, mutate Merchant/provider state, start the OCI
campaign, or advance a logical-day frontier.

Crash execution/orchestration and the full day controller remain separate
implementation slices.
