# Day-wide normal online traffic coordinator (#318)

campaign_day_online_traffic.py coordinates the non-fault online lane for one
deterministic payment-reference logical day.

Scope is intentionally narrower than terminal-day execution. The deterministic
traffic plan contains both normal and fault-overlay slots. This coordinator:

- executes every non-fault slot exactly once through campaign-online-pilot.py;
- reuses the pilot write-ahead intent-before-network contract;
- re-verifies a previously completed slot before treating it as resumable;
- refuses an incomplete, malformed or identity-drifted retained slot;
- retains one create-only deterministic normal-traffic.summary.json;
- never executes a fault-overlay slot;
- never installs or advances a fault plan;
- never runs recovery, reconciliation, settlement or quiescence;
- never writes a terminal day record or advances the campaign frontier.

The current campaign contract derives the normal/fault counts from the frozen
traffic geometry. The coordinator does not carry another hardcoded copy of
those counts.

## Resume boundary

A process restart may continue only across fully acknowledged normal slots.
Each completed slot is rechecked against the exact day plan, source revision,
provenance hashes, request-body digest, expected HTTP status, Merchant operation
identity/state and retained result.

An intent without a complete response/result is ambiguous. The coordinator
blocks before issuing later business mutations. It never guesses whether the
provider committed and never retries a retained attempted mutation.

The shared Merchant/customer setup must also be complete and identical before
another slot can run.

## Fault boundary

The summary decision is:

NORMAL_TRAFFIC_COMPLETE_FAULT_OVERLAYS_PENDING

and day_completion_authorized is always false.

The pending fault-overlay slot numbers and fault classes are retained in the
summary. A later fault/crash orchestration unit must own those slots using the
qualified fault catalog and fault proxy. Completing the normal lane cannot be
used as evidence that all 100 primary lifecycles completed.

This component also does not claim canonical ordering across the combined normal
and fault lanes. Slot numbers remain deterministic identities. The later
fault/crash and terminal-day orchestration layers must establish the complete
100-slot execution contract before terminal acceptance.

## Evidence

The coordinator reuses the existing pilot day journal under the explicit
operator evidence root:

<campaign>-dayNNNNNN/
  identity.json
  setup/
  slot-NNN/
  normal-traffic.summary.json

The canonical campaign days directory is not modified by this component. The
future terminal-day controller must bind online execution evidence to the
independent recovery, reconciliation, provider-cardinality, settlement and
financial-invariant proofs before any terminal day is accepted.

## Execution gate

Plan-only mode performs no HTTP mutation.

Execution requires the same exact source/provenance/live-topology gate as the
one-slot pilot: clean expected repository SHA, immutable provenance documents,
the qualified eight-service Compose runtime, exact container/image identities,
zero restart/OOM drift, and the loopback Merchant endpoint.

Example:

python3 payment-reference/deploy/longrun/campaign_day_online_traffic.py \
  --campaign-id <campaign-id> \
  --day 1 \
  --execute-normal-day \
  --expected-source-sha <40-hex-sha> \
  --provenance-dir <qualified-provenance-dir> \
  --evidence-root <durable-online-journal-root>

This is not a command for fault execution or campaign acceptance.
