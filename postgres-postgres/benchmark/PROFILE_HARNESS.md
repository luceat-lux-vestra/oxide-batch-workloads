# OxideBatch 0.6.0 profiling-harness contract

Campaign: #266  
Primary Track: #13  
Cross-track context: #14, #18, #19, #203

This document fixes the Phase 0 contract for the reusable allocator and
memory-profiling harness exercised against the existing `postgres-postgres`
workload. It is deliberately narrower than the complete #266 campaign: this
unit defines the baseline, claim boundaries, lane separation, and evidence
shape only. Executable allocator selection, profiler integration, workflows,
and retained measurements belong to later bounded units.

## Subject and provenance

The validation subject is the exact published crates.io dependency already
owned by this workload:

```toml
oxide-batch = { version = "=0.6.0", features = ["postgres"] }
```

The committed `postgres-postgres/Cargo.lock` remains authoritative for the
resolved dependency graph. No path dependency, git dependency, workspace
override, vendored framework source, or unpublished OxideBatch artifact may be
used by this harness.

The workload implementation and its existing independent verifier remain the
semantic authority. Profiling support must not fork or weaken the production
read/process/write/checkpoint path merely to make instrumentation easier.

## Representative workload contract

The profiling rehearsal reuses the correctness-complete
`postgres-postgres` workload rather than creating another validation
workload.

For claim-bearing benchmark execution, preserve the canonical comparison
geometry already defined by the existing paired benchmark unless a run is
explicitly labelled diagnostic:

- rows: `1,000,000`;
- seed: `20260904`;
- chunk size: `1000`;
- cursor fetch size: `500`;
- paging page size: `750`;
- warmup pairs: `2` per reader mode;
- measured pairs: `7` per reader mode.

A profiling or smoke run may use a smaller bounded dataset because CPU/heap
instrumentation can be expensive. Its effective parameters must be retained in
the manifest, and its timing must not be compared to the benchmark lane as if
the runs were equivalent.

Database creation, migration, deterministic seeding, teardown, and independent
verification stay outside measured intervals. A sampled or instrumented run
must still pass the existing independent final-state verifier.

## Lane separation

The harness has distinct evidence lanes. A result belongs to exactly one
primary lane; tools may emit supporting metadata into adjacent directories, but
their timing semantics must not be silently mixed.

### Benchmark lane

Purpose: minimally instrumented allocator comparison.

Initial allocator identities:

- `system`;
- `jemalloc`;
- `mimalloc`.

This lane may report wall time, throughput, CPU observations, peak/steady RSS
where available, and correctness. Profiler attachment is forbidden for
authoritative benchmark timing.

Hosted-runner results are same-run observational evidence only; they are not
stable-host absolute-performance truth.

### CPU lane

Purpose: sampled attribution.

Retain the raw sampler output and a reviewable projection such as a call tree
or flame graph. The sampled elapsed time is not benchmark-lane evidence.

### Heap/allocation lane

Purpose: allocation attribution.

Where the selected host supports it, retain allocation stacks, allocation
count, cumulative allocated bytes, live/peak-live contribution, size
distribution, and short-lived allocation observations. Instrumentation overhead
is expected and must be recorded rather than corrected away.

### Timeline/concurrency lane

Purpose: bounded phase and wait attribution.

Only add signals needed to distinguish materially useful boundaries such as
read/process/write/checkpoint, worker/task lifetime, DB wait, queue/backpressure,
and explicit quiescence windows.

### Allocator-native lane

Purpose: allocator-specific state.

Retain native counters when available, including allocated/live, active,
resident, mapped, retained, and relevant arena/bin/tcache state. Do not invent a
single normalized fragmentation metric across allocators with incompatible
semantics.

### OS/process lane

Purpose: process-level resource observation.

Where supported, retain RSS/PSS/smaps-class observations, faults/context
switches, thread/task count, FD count, connection/pool occupancy, and a bounded
resource timeline.

## Build-variant invariant

Allocator variants must differ only in the allocator-selection mechanism and
the minimum dependency/build wiring required to select that allocator.

They must preserve:

- the same OxideBatch `=0.6.0` published subject;
- the same workload source and business semantics;
- the same reader/writer/checkpoint behavior;
- the same release profile and workload parameters for comparable runs;
- the same final-state verifier and correctness bar.

The build-selection unit pins the wrappers used by this rehearsal:

| Identity | Cargo selection | Pinned wrapper/system crate | Upstream allocator provenance |
|---|---|---|---|
| `system` | no allocator feature | Rust/platform default allocator | host/toolchain-owned; no replacement wrapper |
| `jemalloc` | `allocator-jemalloc` | `tikv-jemallocator =0.7.0` | locked `tikv-jemalloc-sys 0.7.0+5.3.1-0-g81034ce1f1373e37dc865038e1bc8eeecf559ce8` identifies jemalloc 5.3.1 |
| `mimalloc` | `allocator-mimalloc` | `mimalloc =0.1.52` | locked `libmimalloc-sys 0.1.49`; wrapper default selects its bundled mimalloc v3 source family |

The two replacement features are mutually exclusive. No feature means
`system`, preserving the pre-campaign workload build. The exact mimalloc
upstream revision is not encoded in the sys-crate version string, so later
manifest generation must record any more precise version only from an
authoritative build/runtime observation rather than infer one here.

## Artifact contract

A run produces a self-contained result root with raw evidence authoritative
over generated summaries:

```text
profile-artifacts/
  manifest.json
  benchmark/
  cpu/
  heap/
  timeline/
  allocator/
  memory/
  figures/
  report/
```

Directories with no supported observation on a host may remain absent, but the
manifest must record the unsupported/unavailable capability and the reason.
Missing evidence must never be rewritten as zero.

At minimum, `manifest.json` records:

- workload commit SHA;
- exact OxideBatch version and registry provenance;
- lockfile identity;
- allocator identity plus wrapper/upstream versions when applicable;
- Rust toolchain and target;
- OS, kernel, CPU, and memory observations;
- build profile and material flags;
- PostgreSQL configured image and observed server version;
- reader mode and all workload/dataset parameters;
- profiler/tool identities and versions;
- lane and run/repetition identity;
- raw artifact inventory;
- independent correctness verdict.

## Long-running reuse boundary

Campaign #203 established durable long-running evidence primitives for the same
published release family. #266 may reuse its event/sample/summary design where
that reduces duplication, but #203 measurements remain #203 evidence.

New allocator/profiling runs must create new evidence and must distinguish:

1. repeated execution;
2. sustained churn/concurrency where exercised;
3. a defined quiescence observation interval;
4. process restart where needed to separate process-local retention from
   durable workload state.

Rising RSS alone is not a leak verdict.

## Claim boundary

This Phase 0 rehearsal may establish that:

- the harness is reproducible against exact published OxideBatch 0.6.0;
- allocator variants execute with unchanged workload correctness semantics;
- provenance and raw artifacts are sufficient for independent review;
- benchmark and profiler lanes remain mechanically distinguishable;
- long-running resource evidence can be extended without reinterpreting #203.

It may not:

- select the permanent/default OxideBatch allocator;
- generalize 0.6.0 results to a post-M7 release;
- use profiler-instrumented timing as benchmark truth;
- present hosted-runner timing as stable-machine absolute performance;
- open optimization work from noisy or unattributed measurements.

## Build-selection smoke

Protected workload CI exercises the three build identities against one small
deterministic PostgreSQL source. Each variant runs the unchanged paging
workload and the unchanged independent verifier under a distinct import name.
The smoke is correctness/build evidence only: its timings are discarded and
must never enter benchmark comparisons.

CI also rejects enabling both replacement allocators together and checks both
optional variants under the workload's declared MSRV.

## Next implementation boundary

Profiler tools, opt-in profiling orchestration, soak execution, allocator-native
telemetry, and retained claim-bearing measurements remain separate later units.
