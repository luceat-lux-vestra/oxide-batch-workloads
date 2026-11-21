# Per-day quiescence resource evidence (#318)

`campaign_quiescence.py` implements the bounded **read-only** quiescence
sampler for one already-completed payment-reference logical day. It does not
run online traffic, fault injection, recovery, reconciliation, settlement,
OxideBatch workloads, or terminal-day/frontier mutations.

## Machine-readable cadence

`quiescence-contract-v1.json` is the source of truth for the sampler rather
than embedding host-specific values in code.

It freezes:

- campaign contract: `payment-reference-longrun-v1`;
- target observation interval: 4 seconds;
- maximum accepted observed gap: 5 seconds;
- at least three PostgreSQL targets discovered from the **resolved Compose
  model**, not from generated container names;
- the `oxide_batch` metadata schema name.

The required window duration is not duplicated in this contract. It is read
from the deterministic campaign day plan:

- ordinary logical day: >=60 seconds;
- every tenth logical day: >=600 seconds.

The separate pre-campaign host-qualification quiescence and final campaign
>=1800-second quiescence remain distinct lifecycle boundaries.

## Retained sample

Each append-only `samples.jsonl` record uses:

`oxide-batch-workloads.payment-reference-resource-sample/v1`

and binds the campaign ID, logical day, source revision, quiescence-contract
SHA-256, deterministic window identity, global sample ordinal, window-local
sample index, UTC observation timestamp, monotonic elapsed time, required
window duration and target cadence.

Every observation retains:

- guest MemTotal/MemAvailable/swap-used and 1/5/15-minute load average;
- evidence/data filesystem total/used/available bytes;
- the exact campaign service set from `campaign.json`;
- container ID/PID, restart count, OOM/running state;
- process RSS/PSS, thread count and FD count where procfs permits;
- cgroup-v2 memory current/peak where available;
- PostgreSQL total/active connection count and database size;
- all visible `oxide_batch` user tables with PostgreSQL's
  `n_live_tup` **estimated** row count plus exact relation total bytes.

Unavailable procfs/cgroup metrics are explicit nulls with an unavailable
reason. A missing expected container, container restart, OOM, stopped
container, missing minimum PostgreSQL topology, malformed observation or
sampling gap beyond the contract blocks the window.

## Compose and source binding

The runtime command requires explicit repository, campaign, data, Compose and
optional Compose-env paths. It verifies:

1. clean repository HEAD equals the campaign's frozen source revision;
2. the resolved Compose configuration hash equals qualified
   `provenance/runtime.json`;
3. the resolved Compose service set equals the exact campaign image service
   set.

PostgreSQL database/user identities are discovered from resolved
`POSTGRES_DB` / `POSTGRES_USER` values and passed directly as argv to
container-local `psql`; no password or database URL is retained.

Example:

```bash
python3 payment-reference/deploy/longrun/campaign_quiescence.py \
  --campaign-dir '<campaign evidence directory>' \
  --repo-root '<exact clean repository checkout>' \
  --data-path '<durable campaign data root>' \
  --compose-file '<qualified Compose file>' \
  --compose-env-file '<qualified optional Compose env file>' \
  --day N
```

## Fail-closed replay

A completed retained window is an idempotent read-only replay: the command
verifies and reports the existing evidence without appending new bytes.

If a process exits after a window has started but before a valid end marker is
retained, the same logical-day window is **not** silently resumed or replaced.
The retained incomplete evidence blocks replay and requires explicit lifecycle
handling by the future controller/operator.

The sampler itself cannot prove that another actor refrained from mutations.
The future terminal-day controller must own the phase boundary that starts this
command only after all day-close work has stopped and must not launch any
business/batch mutation until quiescence verification has completed.
