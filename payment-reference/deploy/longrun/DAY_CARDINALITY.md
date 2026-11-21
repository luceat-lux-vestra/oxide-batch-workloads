# Day-wide provider effective-side-effect cardinality proof (#318)

This is an independent, read-only 100-slot cardinality stage. It is **not**
controller execution, day PASS, attempt counting, OCI qualification, or a
statement/settlement mutation.

## Authorities and closure

1. Re-derive the deterministic 100-slot traffic plan for the exact campaign/day.
2. Read the retained immutable settlement roster and its 100-slot verification.
   Revalidate them using the existing roster builder before any provider read.
3. For every slot, use the roster's Gateway-derived provider-payment identity.
4. Read the authenticated payment-sandbox v1.5.4
   `GET /admin/payments/{id}/timeline` **inside payment-sandbox**.
5. Require the full provider-created/event/version/causation ledger contract;
   recompute exact effective mutations and monetary terminal state.
6. Compare against plan-derived expectations, the roster's provider snapshot,
   and all other provider identities in the day. Preserve raw timelines and the
   full per-payment proof for offline verification.

The expected effective mutation counts in the current frozen geometry are:

| Plan class | Slots | Effective provider mutations per slot |
| --- | ---: | ---: |
| purchase (auth-capture) | 40 | 2 |
| authorize + two captures | 25 | 3 |
| authorize + cancel | 10 | 2 |
| auth-capture + two refunds | 15 | 4 |
| fault overlay | 10 | 0 (before-effect 503) or 1 (other qualified classes) |

Current day total: **243 provider effective mutations**, with exactly two
provider-absent HTTP-404 proofs. The total is recomputed from all 100
individual slot contracts, never substituted as a standalone aggregate check.

The fault class is read from the frozen fault catalog; the provider status
for each overlay must match that catalog. A 404 is valid only for the
catalog's pre-side-effect case. Non-200/non-404 statuses fail closed.

## Immutable output

`statements/day-NNNNNN.provider-cardinality.verification.json`

Schema:
`oxide-batch-workloads.payment-reference-day-provider-cardinality-verification/v1`

Decision: `PROVIDER_DAY_CARDINALITY_VERIFIED_NOT_DAY_ACCEPTED`.

The retained artifact includes:

- campaign ID, logical day, source revision, plan SHA;
- canonical roster digest and **exact retained roster-verification digest**;
- pinned provider authority and payment-sandbox source revision;
- exactly 100 ordered slot observations;
- per-slot Merchant and Provider IDs, kind, fault class, HTTP status, raw
  authoritative timeline (null for true 404), and the independently validated
  per-payment provider cardinality result;
- total effective mutation count, recomputed across all slots.

The producer uses create-only immutable retention: byte-identical replay is
accepted, conflicting evidence cannot overwrite an existing proof.

## Run only after independently verified roster

```bash
python3 payment-reference/deploy/longrun/campaign_day_cardinality.py \
  --campaign-dir /srv/oxide-batch-workloads/evidence/<campaign-id> \
  --day 1
```

This performs **read-only** provider timeline requests via the existing
payment-sandbox Compose service and retains evidence only. It does not call any
payment mutation endpoint, run a batch workload, or append a terminal-day event.

## Frontier binding

The `side_effect_cardinality` check in day-independent verification v2 must
provide the same bound descriptor shape as settlement checks, with the exact
day-scoped filename, artifact schema/version, campaign/day/source identity,
retained-file SHA-256, and independently recomputed subject:

- `observed_slot_count`
- `effective_mutation_total`
- `roster_sha256`
- `roster_verification_sha256`
- `provider_source_revision`

The frontier scanner replays all 100 retained raw provider timelines through
the already qualified per-payment verifier, verifies roster slot mappings,
expected transitions, absent-provider proofs, source/plan hashes, and the
roster-verification cross-link before accepting a cardinality check.

A controller-authored `PASS + SHA` placeholder is insufficient.

## Evidence boundaries

The retained authenticated-read response was observed by the independent
producer; offline replay verifies *internal proof consistency*, not cryptographic
attestation by payment-sandbox. A fabricated raw JSON file cannot be proven
authentic without an external trust/attestation channel.

Likewise, provider mutations are not **request attempts**. Gateway, Kill Bill,
fault proxy, and controller execution histories have separate authorities.
The remaining recovery/reconciliation/financial-invariant check contracts
require their own independent work before a real terminal day can be accepted.

## Local tests

```bash
python3 payment-reference/deploy/longrun/test-campaign-day-cardinality.py
python3 payment-reference/deploy/longrun/test-campaign-evidence.py
```

Tests use fabricated provider timelines only, do not contact provider services,
and cannot generate a real campaign PASS.
