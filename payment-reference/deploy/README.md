# Third-party deployment pins

This directory freezes the external component inputs selected or qualified for
payment reference v1 and contains the bounded Kill Bill -> payment-sandbox
runtime topology. Merchant/Gateway orchestration is still outside this runtime
checkpoint.

The source of truth is `third-party.lock.json`.

## Active v1 pins

- Kill Bill 0.24.22.
- payment-sandbox v1.5.4.
- PostgreSQL 18.6.

## Qualified but deferred inputs

- Hyperswitch v1.129.0 standalone router.
- Redis 7.4.12 for a future Hyperswitch interoperability lane.
- Card Vault v0.9.0.
- `juspay/hyperswitch-killbill-plugin` source was qualified and rejected for
  unchanged v1 use.
- `deepanshu-iiitu/killbill-prism-plugin` was reviewed as an interoperability
  candidate and is not selected for v1.

Floating `latest`, floating Git branches and unverified payment-sandbox
downloads are forbidden.

## Runtime topology

`compose.yaml` is intentionally bounded to:

```text
127.0.0.1:8080
      |
      v
  Kill Bill 0.24.22
      |
      +-- JDBC --> PostgreSQL 18.6
      |              + Kill Bill 0.24.22 DDL
      |              + payment-sandbox plugin DDL
      |
      v
repository-owned PaymentPluginApi
      |
      v
payment-sandbox v1.5.4
```

PostgreSQL and payment-sandbox are not published to the host. The only default
host listener is Kill Bill on loopback.

The Kill Bill image is pinned to the qualified 0.24.22 image-index digest.
The DB image remains the separately qualified `postgres:18.6-bookworm` input.
Its Kill Bill schema is assembled from the exact 0.24.22 source commit; every
downloaded DDL is verified against its Git blob SHA before it is admitted to the
image.

The plugin is built with `mvn verify` and installed into the Kill Bill bundle
tree through KPM during the derived Kill Bill image build.

Run the bounded smoke qualification with:

```bash
bash payment-reference/deploy/smoke.sh
```

The smoke requires Kill Bill health, payment-sandbox readiness and observable
OSGi registration of `killbill-payment-sandbox`.

Hyperswitch remains intentionally deferred. Reintroducing it later requires a
specific interoperability claim and executable proof that the additional
stateful layer adds validation value.

## payment-sandbox image

`payment-sandbox/Dockerfile` builds a small runtime image from the exact
v1.5.4 published Linux binary. Both amd64 and arm64 asset hashes are embedded
and checked before installation.

This deliberately avoids building an evolving upstream `main` checkout.

## Evidence rule

Before any official campaign, resolve every container tag used by that campaign
to the exact runtime image digest on the target registry/architecture and retain
those digests in campaign provenance. A release tag is sufficient for
development bootstrap but not for final evidence.
