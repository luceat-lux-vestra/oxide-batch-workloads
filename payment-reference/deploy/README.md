# Third-party deployment pins

This directory freezes the external component inputs selected or qualified for
payment reference v1. It does not claim that the complete stack is runnable
yet.

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

## Why there is no full compose file yet

Source qualification changed the v1 bridge decision.

The first runnable Billing -> provider path will use a repository-owned thin
Kill Bill `PaymentPluginApi` adapter directly against the pinned
`payment-sandbox` HTTP API. See `bridge-qualification.md`.

A full Compose topology would be premature until that adapter has an executable,
tested artifact. No separate Hyperswitch plugin fork is required for this
checkpoint.

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
