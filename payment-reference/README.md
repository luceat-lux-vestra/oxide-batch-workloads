# Payment reference system

Issue owner: `#256` under coordination Epic `#253`.

This directory hosts a realistic payment-system reference environment used to
create business state that later OxideBatch workloads can reconcile. It is not
itself an OxideBatch validation workload yet and therefore must not be
registered under `workloads` until an exact published OxideBatch consumer is
introduced.

## v1 topology

```text
Deterministic Customer Scenario
  -> Reference Merchant
  -> Thin Gateway / UPP
  -> Kill Bill
  -> Hyperswitch
  -> payment-sandbox
```

The first scaffold intentionally contains only repository-owned contracts and
service boundaries. Third-party deployment pins and adapters arrive in later
bounded checkpoints.

## Non-negotiable boundaries

- Merchant-facing APIs are provider-neutral.
- Money is integer minor units plus explicit three-letter currency.
- Merchant, Gateway, Kill Bill, Hyperswitch and provider identifiers stay
  distinct and are mapped explicitly.
- Transport failure is not equivalent to business failure.
- `UNKNOWN` is a first-class operation outcome for ambiguous external effects.
- Same idempotency key + conflicting canonical mutation is rejected.
- No workload may query undocumented Kill Bill/Hyperswitch internal tables.
- External OSS is consumed as pinned artifacts, never Git submodules.
- No real PAN/CVV or live PSP credential belongs in this system.

## Current scaffold

- `api/openapi.yaml` — canonical Merchant -> Gateway HTTP surface.
- `crates/payment-contract/` — provider-neutral domain and identity contract.
- `merchant/` — reference Merchant process boundary.
- `gateway/` — thin Gateway/UPP process boundary.
- `scenarios/v1/` — deterministic scenario identity contract.

## Deferred from this candidate HEAD

- Merchant PostgreSQL persistence and migrations.
- HTTP server implementation.
- Kill Bill/Hyperswitch/payment-sandbox deployment pins.
- Kill Bill <-> Hyperswitch bridge qualification in executable tests.
- Hyperswitch -> payment-sandbox connector.
- End-to-end payment lifecycle.
- broader deterministic fault matrix.
- OxideBatch workloads.
