# Canonical provider settlement roster builder (#318)

The per-day settlement roster is an **independent read-only derivation**, not a
controller-owned list.

## Authority chain

For every one of the 100 deterministic traffic slots:

```text
campaign day plan
  -> merchantPaymentId
  -> Gateway persisted providerPaymentId mapping
  -> direct payment-sandbox GET /payments/{providerPaymentId}
  -> expected provider truth for that exact normal/fault case
  -> settlement eligibility
```

The builder does not use Kill Bill transaction counts, controller intent, or
fault-proxy forwarding counters as provider settlement truth.

Normal cases require both the plan-defined Gateway terminal state with `lastOperationState=SUCCEEDED` and the plan-defined direct provider state:

- auth-capture purchase -> `captured`;
- authorize + capture -> `captured`;
- authorize + cancel -> `cancelled`;
- auth-capture + refund -> `refunded`.

Fault-overlay cases are checked against the canonical fault catalog. The
pre-side-effect 503 case must have no provider aggregate (direct provider HTTP
404). The remaining qualified cases must match their catalog provider truth
(`authorized` or `failed`). Any unexpected captured/refunded fault outcome
blocks roster creation instead of silently adding settlement membership.

Only direct provider `captured` and `refunded` states are eligible. With the
frozen 40/25/10/15/10 daily traffic geometry, a fully successful current day
therefore has exactly 80 settlement members; the builder derives that count
rather than hard-coding it.

## Immutable outputs

The builder creates two independent artifacts:

- `settlement-roster-v1` — the existing statement-producer contract containing
  the sorted provider payment membership;
- `payment-reference-settlement-roster-verification` — all 100 observed slot
  mappings/provider states plus plan/contract/fault-catalog/roster digests.

Both are create-only. Exact replay compares byte-identical canonical JSON and
does not rewrite the original inode. Different content at the same retained
path is a hard conflict.

The roster uses:

- campaign identity `source_revision`;
- logical business date from the deterministic day plan;
- day seed as `scenario_seed`;
- the operator-selected provider account identity;
- sorted unique provider payment IDs.

## Statement integration

No new statement format is introduced. The existing
`produce-provider-settlement-statement.py` consumes the builder's roster
unchanged and performs another direct provider read before producing statement
lines/totals. The roster fixes **membership**; the provider statement fixes the
settlement amounts/version observed at statement production.

## Runtime use

After campaign initialization and after all day traffic/fault outcomes have
been independently converged:

```bash
python3 payment-reference/deploy/longrun/campaign-settlement-roster.py \
  --campaign-dir /srv/.../campaign-318 \
  --day 1 \
  --provider-account-id payment-sandbox-default \
  --output /srv/.../statements/day-000001.roster.json \
  --verification-output /srv/.../statements/day-000001.roster.verification.json
```

Only HTTP GETs are issued. Gateway must be reached through its configured
loopback port and provider truth through the payment-sandbox direct loopback
port, never through the fault proxy.

Successful output is
`SETTLEMENT_ROSTER_VERIFIED_NOT_DAY_ACCEPTED`; it cannot finalize a day or
advance campaign evidence by itself.
