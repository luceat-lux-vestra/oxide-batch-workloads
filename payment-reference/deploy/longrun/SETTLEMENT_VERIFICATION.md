# Independent provider statement + Merchant close verification (#318)

This stage closes the settlement proof gap without making the future campaign
controller an authority.

## Inputs

The verifier consumes:

- the initialized campaign identity and deterministic logical day;
- the immutable `settlement-roster-v1`;
- the immutable 100-slot settlement-roster verification from
  `campaign-settlement-roster.py`.

It validates the roster verification against the exact deterministic day plan
before any settlement close mutation.

## Provider statement

The existing `produce-provider-settlement-statement.py` remains the statement
authority. The verifier:

1. re-reads every roster member directly from payment-sandbox;
2. produces and validates `provider-settlement-statement-v1`;
3. publishes the statement create-only;
4. rejects any same-path replay whose immutable statement content changed.

The statement binds the roster by `source_roster_digest`, exact provider account/date/currency identity, and exact sorted provider-payment membership; it also retains provider versions plus captured/refunded/net totals.

## Merchant close reconciliation

Before POSTing `/settlements/close`, the verifier reads Merchant's persisted
settlement state directly from `merchant-db`.

This preflight is required because a previous attempt may have committed and
lost its HTTP response. The rules are:

- no row + no close event: a first close POST may be sent;
- exact CLOSED row + exactly one `SETTLEMENT_CLOSED` event: reconcile as the
  already-authoritative statement and proceed only with exact replay;
- any different row, identity collision, HELD state, duplicate event, or orphan
  event: block before another close mutation.

After authoritative close exists, the verifier proves all of the following:

- exact replay returns HTTP 200 with `replayed=true`;
- replay does not change the persisted row or event cardinality;
- same statement identity with a different valid digest is rejected HTTP 409;
- same statement identity with different internally valid totals is rejected
  HTTP 409;
- both negative controls leave the original CLOSED row unchanged;
- final exact replay still returns the original statement;
- exactly one Merchant `SETTLEMENT_CLOSED` business event exists.

The DB reads are read-only and use the Compose service `merchant-db`, never a
generated container name.

## Stable verification artifact

The retained verification artifact is deliberately independent of whether the
first invocation created the Merchant row or reconciled an already-created
row. Therefore a post-commit response loss followed by a later verifier rerun
produces the same final artifact.

It binds:

- campaign ID, logical day, source revision;
- roster digest;
- roster-verification digest;
- provider statement identity, immutable statement digest/artifact digest and
  totals;
- exact persisted Merchant CLOSED row;
- close-event cardinality = 1;
- replay HTTP 200;
- digest-conflict HTTP 409;
- totals-conflict HTTP 409;
- authoritative state unchanged after both negative controls.

A success decision is:

`SETTLEMENT_VERIFIED_NOT_DAY_ACCEPTED`

This artifact can later satisfy the independent settlement-close proof input,
but it is not itself a day PASS, campaign PASS, controller event, or frontier
advance.

## Runtime invocation

After the per-day roster has been independently created:

```bash
python3 payment-reference/deploy/longrun/campaign-settlement-verification.py \
  --campaign-dir /srv/.../campaign-318 \
  --day 1 \
  --roster /srv/.../statements/day-000001.roster.json \
  --roster-verification /srv/.../statements/day-000001.roster.verification.json \
  --statement-output /srv/.../statements/day-000001.statement.json \
  --verification-output /srv/.../statements/day-000001.settlement.verification.json
```

Provider and Merchant endpoints are restricted to their configured loopback
ports. Existing OCI Compose/env selection continues through
`PAYMENT_REFERENCE_COMPOSE_FILE` and
`PAYMENT_REFERENCE_COMPOSE_ENV_FILE`.
