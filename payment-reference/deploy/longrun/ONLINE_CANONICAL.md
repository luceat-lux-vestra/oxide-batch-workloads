# Canonical normal + fault online execution (#318)

`campaign_day_online_canonical.py` is the primary-lifecycle ordering authority
for a real payment-reference logical day.

The older normal-lane and fault-lane coordinators remain useful bounded
development tools, but their outputs cannot prove one combined execution order.
Running all normal slots and then all fault slots and later sorting evidence by
slot number would manufacture an order that did not occur. This controller
therefore starts only from a day that has no primary slot evidence and no
lane-summary evidence.

## Canonical order

The deterministic traffic plan already defines the complete primary lifecycle
sequence. The controller executes exactly that `slots` array in order,
including fault overlays at their real positions.

Before the first primary mutation it creates:

```text
<campaign>-dayNNNNNN/
  online-order/
    identity.json
```

For every slot it then retains:

```text
slot-NNN.intent.json
slot-NNN.complete.json
```

The intent is fsynced before that slot is allowed to mutate Merchant/provider
state. The completion is written only after the existing normal/fault slot
validator accepts the retained slot evidence.

The first intent contains the SHA-256 of the journal identity. Every later
intent contains the SHA-256 of the preceding completion. Each completion
contains the intent digest and a digest map of every retained file in the slot
evidence directory. The resulting identity -> slot 1 -> ... -> slot N chain
proves the controller-observed order; it is not reconstructed from slot
numbers after the fact.

An intent without a completion is an ambiguous attempted financial slot.
Automatic replay is forbidden. A later slot, a completion without its intent,
unexpected journal files, a broken hash chain, or slot evidence outside the
journal prefix all block the day before another primary mutation.

## Legacy lane boundary

A day containing either:

- pre-existing `slot-NNN/` primary evidence without the canonical journal;
- `normal-traffic.summary.json`; or
- `fault-traffic.summary.json`

cannot be promoted into canonical-order evidence.

The normal/fault lane coordinators also refuse to mutate a day once
`online-order/` exists. All day-level online coordinators share one
root-level controller lock, while the existing day `.lock` continues to
serialize the underlying slot journal mutations.

The low-level fault slot executor/validator no longer requires a completed
normal-lane summary. The **fault-lane coordinator still does**; this separation
allows the canonical controller to place a fault slot at its actual plan
position without weakening the legacy lane contract.

Low-level normal/fault mutation entrypoints do not trust a caller ownership
flag. If `online-order/` exists, they derive authority from retained evidence:
the exact journal identity must match campaign/day/source/provenance/plan, the
exact slot intent must already exist and match that plan item, the current slot
must have no completion yet, and every non-first slot must chain to a durable
preceding completion. Therefore the canonical controller cannot call a primary
mutation before its exact fsynced order intent exists, and another caller cannot
opt into canonical ownership by passing a boolean.

## Output authority

After every planned primary lifecycle has a validated chained completion, the
controller retains `online-traffic.summary.json` with:

- the exact campaign/day/source/provenance and plan identities;
- all slot completion hashes in canonical plan order;
- the final order-chain head;
- derived normal/fault counts;
- the scheduled hard-crash contract;
- whether later recovery/reconciliation is required;
- `combined_normal_fault_ordering_authorized=true`;
- `online_completion_authorized=true`;
- `day_completion_authorized=false`.

This is **online execution authority only**. It does not run or accept
recovery, reconciliation, provider cardinality, financial invariants,
settlement close, quiescence, hard-crash recovery, terminal day verification,
or campaign-frontier advancement.
