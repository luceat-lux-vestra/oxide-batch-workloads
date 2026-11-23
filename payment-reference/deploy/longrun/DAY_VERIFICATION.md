# Canonical day-independent verification contract (#318)

A terminal campaign day may advance the evidence frontier only from retained,
independently revalidated proof. Controller-authored PASS flags are never an
acceptance authority.

## Canonical contract boundary

`oxide-batch-workloads.payment-reference-day-independent-verification` uses
`schema_version: 1` as the single current schema.

The campaign has no external consumer that requires compatibility with
development-era day-verification shapes. The frontier therefore implements only
the current canonical contract and requires exactly **eight** bound proof
descriptors:

- `settlement_roster`
- `settlement_close`
- `side_effect_cardinality`
- `recovery`
- `reconciliation`
- `financial_invariants`
- `canonical_online`
- `quiescence`

Each descriptor binds PASS, exact artifact schema/version, campaign/day/source,
a safe campaign-relative retained file, its exact SHA-256, and a subject that
the frontier derives again from the retained proof.

## Canonical online proof

`campaign_day_online_verification.py` is a read-only verifier over the
separate durable canonical-online journal. It does not trust
`online_completion_authorized=true` by itself.

Before retaining
`days/day-NNNNNN.online.verification.json`, it independently replays:

- exact online day identity bound to campaign source and all four provenance
  digests;
- canonical journal identity;
- all deterministic slot intents in plan order;
- the identity -> slot 1 -> ... -> slot N completion hash chain;
- every retained normal/fault slot through the existing read-only slot
  validators;
- every completion's exact slot-evidence digest map;
- exact journal file membership;
- the immutable `online-traffic.summary.json` and its authority boundary.

The compact proof retains the exact journal identity, all 100 intent/completion
objects and the online summary. The frontier replays that compact chain again
from the deterministic plan and re-derives the descriptor subject. Therefore a
boolean, reordered slot list, forged intent, broken predecessor digest,
completion/evidence mismatch, source/provenance mismatch or summary tamper
cannot advance the frontier.

The raw online journal remains outside the campaign acceptance directory. The
terminal layer binds only the immutable independent proof; it never stores or
trusts an operator path as acceptance authority.

## Quiescence proof

`campaign_day_quiescence_verification.py` compacts one completed window from
the append-only campaign `samples.jsonl` into
`days/day-NNNNNN.quiescence.verification.json`.

The producer reuses `campaign_quiescence.verify_window` and binds:

- exact campaign/day/source;
- deterministic day-plan digest;
- exact quiescence-contract digest;
- verifier-source digest;
- digest of the exact ordered window records;
- first/last sample ordinals;
- observed duration and maximum sample gap;
- stable PostgreSQL target set.

The frontier does more than validate the compact artifact: it re-reads the
current append-only `samples.jsonl`, extracts the exact day window, replays
`verify_window`, recomputes the record digest and compares the compact
subject. Later days may append samples without changing an earlier window's
identity.

This proves the required per-day 60-second / every-tenth-day 600-second window.
The campaign-final >=1800-second quiescence remains a separate final-acceptance
boundary and is not claimed by this per-day contract.

## Existing six proof families

Settlement roster/close, provider side-effect cardinality, recovery,
reconciliation and financial-invariant semantics retain their existing semantics. Their
safe relative-path, retained-file SHA, schema, source identity and cross-system
financial consistency checks are unchanged.

See:

- [DAY_CARDINALITY.md](DAY_CARDINALITY.md)
- [DAY_RECOVERY.md](DAY_RECOVERY.md)
- [DAY_RECONCILIATION.md](DAY_RECONCILIATION.md)
- [DAY_FINANCIAL_INVARIANTS.md](DAY_FINANCIAL_INVARIANTS.md)

## Frontier authority

Lifecycle events remain sequencing records, not proof. A future terminal-day
controller may emit events and assemble descriptors, but
`campaign-evidence.py::inspect()` independently rejects a terminal success
unless all eight canonical proofs are present and valid.

This contract still does not:

- create terminal day records;
- execute traffic/recovery/reconciliation/settlement;
- implement the terminal-day orchestrator;
- enforce the campaign-wide >=24h / >=30-day / >=3000-flow floor;
- enforce final >=1800-second quiescence;
- advance campaign acceptance beyond the existing per-day frontier semantics.
