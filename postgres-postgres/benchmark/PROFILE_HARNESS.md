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

Phase 0 initial allocator identities were:

- `system`;
- `jemalloc`;
- `mimalloc`.

The Phase 1 registry now additionally qualifies `snmalloc` and `tcmalloc`
on ARM64 Linux. Candidate-screen expansion does not turn this lane into a
default-allocator recommendation.

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

## Allocator candidate registry

`benchmark/allocator_registry.json` is the canonical selection metadata for
the profiling harness. Producer code, manifest validation, CI smoke selection,
MSRV feature checks, and mutual-exclusion checks consume the same registry
instead of maintaining independent allocator lists.

The registry records, per candidate:

- allocator identity and lifecycle status;
- screening tier;
- Cargo feature;
- wrapper crate/version;
- exact lockfile package/version requirements;
- upstream-version observation state;
- target qualification state;
- build prerequisites;
- telemetry wiring state.

The registry is configuration, not benchmark evidence. A target marked
`qualified` means this harness has a supported execution path on that target;
it does not mean the allocator is faster or preferred.

New candidates must first enter the registry with an explicit target state.
They must not be enabled merely because the upstream allocator supports a
platform: the Rust wrapper/build path must also be qualified. Producer and CI
selection are filtered by the exact Rust host target, and manifests fail closed
if an allocator is not qualified for the recorded target.

## Build-variant invariant

Allocator variants must differ only in the allocator-selection mechanism and
the minimum dependency/build wiring required to select that allocator.

They must preserve:

- the same OxideBatch `=0.6.0` published subject;
- the same workload source and business semantics;
- the same reader/writer/checkpoint behavior;
- the same release profile and workload parameters for comparable runs;
- the same final-state verifier and correctness bar.

The registry currently activates the qualified ARM64 Linux candidate set:

| Identity | Cargo selection | Pinned wrapper/system crate | Upstream allocator provenance |
|---|---|---|---|
| `system` | no allocator feature | Rust/platform default allocator | host/toolchain-owned; no replacement wrapper |
| `jemalloc` | `allocator-jemalloc` | `tikv-jemallocator =0.7.0` | locked `tikv-jemalloc-sys 0.7.1+5.3.1-0-g81034ce1f1373e37dc865038e1bc8eeecf559ce8` identifies jemalloc 5.3.1 |
| `mimalloc` | `allocator-mimalloc` | `mimalloc =0.1.52` | locked `libmimalloc-sys 0.1.49`; wrapper default selects its bundled mimalloc v3 source family |
| `snmalloc` | `allocator-snmalloc` | `snmalloc-rs =0.7.6` | locked `snmalloc-sys 0.7.6`; wrapper tracks upstream snmalloc 0.7.6 |
| `tcmalloc` | `allocator-tcmalloc` | `tcmalloc-better =0.1.19` | locked `libtcmalloc-sys 0.1.14`; exact vendored Google TCMalloc revision is not inferred from wrapper/sys versions |

### Phase 1 candidate queue

Candidate registration is deliberately separate from dependency activation.
`planned` and `conditional` entries describe what should be qualified next;
they are excluded from producer/CI execution and cannot appear in retained
manifests until promoted to `active` with exact lockfile provenance and at
least one `qualified` target.

| Identity | Lifecycle | Wrapper | Target state | Qualification note |
|---|---|---|---|---|
| `snmalloc` | `active` / Phase 1 | `snmalloc-rs 0.7.6` | ARM64 Linux: `qualified`; x86_64 Linux: `supported-unverified` | exact published wrapper/sys 0.7.6 is locked; default CMake build is qualified by repository CI and the allocator smoke on ARM64 before any performance claim |
| `tcmalloc` | `active` / Phase 1 | `tcmalloc-better 0.1.19` | ARM64 Linux: `qualified`; x86_64 Linux: `supported-unverified` | default 8k-page/extension wrapper build is locked; workload entry points explicitly invoke the wrapper background-action lifecycle before qualification evidence is accepted |
| `rpmalloc` | `conditional` | `rpmalloc 0.2.2` | ARM64 Linux: `unsupported`; x86_64 Linux: `supported-unverified` | Rust wrapper documents tested support only on x86_64 Windows/macOS/Linux, so it is not an ARM64 campaign candidate without separate wrapper qualification |

Empty `lock_packages` is valid only while a replacement allocator is not
`active`. Activation requires Cargo dependency resolution in this repository,
exact locked package provenance, build/correctness qualification on the target,
and an explicit registry state change.

All active replacement allocator features are pairwise mutually exclusive. No feature means
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
- Rust compiler and Cargo versions, plus target;
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

Protected workload CI exercises every registry-qualified active build identity
for the exact host target against one small deterministic PostgreSQL source. Each variant runs the unchanged paging
workload and the unchanged independent verifier under a distinct import name.
The smoke is correctness/build evidence only: its timings are discarded and
must never enter benchmark comparisons.

CI also rejects enabling both replacement allocators together and checks both
optional variants under the workload's declared MSRV.

## Executable allocator smoke producer

`benchmark/profile_smoke.py` is the first executable consumer of the manifest
contract. It deliberately remains a **diagnostic smoke**, not a claim-bearing
allocator benchmark.

For one selected reader mode it:

1. builds every registry-qualified active allocator variant for the host target in release mode;
2. creates one deterministic template database and clones an isolated database
   per allocator;
3. runs the unchanged workload path for each variant;
4. runs the unchanged independent verifier;
5. records bounded GNU-time/process observations and the verifier JSON as raw
   artifacts;
6. emits one `manifest.json` per allocator and validates it through
   `profile_manifest.py`.

The result bundle is shaped as:

```text
profile-artifacts/
  system/
    manifest.json
    benchmark/
      measurement.json
      verifier.json
  jemalloc/
    ...
  mimalloc/
    ...
  snmalloc/
    ...
  tcmalloc/
    ...
```

The producer rejects non-empty output roots so evidence from separate runs
cannot be silently mixed. Its smoke dataset is bounded to at most 100,000 rows.
Every manifest explicitly states that the timing is diagnostic, has no profiler
attached, and is not stable-host allocator-selection evidence.

The manual-only workflow
`.github/workflows/profile-postgres-postgres-smoke.yml` runs this matrix on the
repository's ARM64 Ubuntu runner and uploads the bundle for review. Ordinary PR
CI runs only the dependency-free producer unit tests; it does not perform the
release smoke matrix.

## Manifest schema evolution

Retained Phase 0 evidence may use either:

- `oxide-batch-profile-manifest/v1` — original contract; Cargo version was not
  recorded and remains valid historical evidence;
- `oxide-batch-profile-manifest/v2` — current producer contract; adds exact
  `subject.cargo` provenance while preserving the remaining v1 semantics.

The validator accepts both versions so previously retained v1 artifacts remain
independently reviewable. New producer runs emit v2 only. A v2 manifest without
an exact Cargo version fails closed.

This distinction matters for Rust/Cargo 1.100 and later because build-directory
selection can vary with build configuration and feature state; allocator
variants must therefore retain the exact Cargo version alongside `rustc`.

## Repeated allocator benchmark producer

`benchmark/allocator_benchmark.py` is the benchmark-lane producer for the
registry-qualified allocator matrix. It is intentionally separate from the
diagnostic smoke producer and from CPU/heap profiler orchestration.

The canonical geometry remains the previously fixed benchmark contract:

- both cursor and paging readers;
- 1,000,000 source rows with seed 20260904;
- chunk size 1000;
- cursor fetch size 500;
- paging page size 750;
- 2 warmup rounds per reader;
- 7 measured rounds per reader.

Each allocator sample receives a fresh clone of one deterministic template
database. Setup, cloning, teardown, and independent verification remain outside
the timed interval. Warmups execute the same correctness path but are excluded
from measured distributions.

Allocator order rotates deterministically across rounds. Warmup and measured
order sequences are independent, so warmups cannot shift the measured
position-balance schedule. One complete measured cycle places every allocator
in every order position once; alternating cycles reverse the rotation
direction. The exact warmup and measured order is retained in the summary
artifact.

Each measured sample emits its own validated benchmark-lane manifest plus raw
measurement and verifier artifacts. The top-level summary retains
min/median/max/p95 distributions for elapsed time, throughput, user/system CPU,
and peak RSS for every allocator/reader pair.

Only the exact canonical geometry is labelled
`same-host-relative-observational`. Any bounded override is labelled
`diagnostic-noncanonical` and every sample manifest records that it is not
campaign acceptance evidence. Hosted-runner numbers remain unsuitable for
stable-host absolute-performance claims, and this 0.6.0 rehearsal cannot select
the post-M7 default allocator.

## CPU attribution producer

`benchmark/cpu_profile.py` is the first CPU-lane producer. It keeps profiling
mechanically separate from the benchmark lane and profiles exactly one
registry-qualified allocator/reader selection at a time.

The producer:

- builds the normal release workload binary for the selected allocator; it
  does not add an in-process profiler dependency or change default builds;
- attaches Linux `perf record` externally with frequency sampling and DWARF
  call chains;
- passes the credential-bearing PostgreSQL URL through `DATABASE_URL`, never
  the recorded child argv;
- retains raw `cpu/perf.data`, textual `perf script` stacks, a reviewable
  `perf report --stdio` projection, profiler metadata, and the independent
  workload verifier;
- emits a validated `lane=cpu` manifest with `profiler_attached=true` and
  no `benchmark_timing` field;
- rejects an unavailable profiler, an unqualified allocator/target, empty
  profile/projection output, profiler failure, or failed correctness
  verification.

Default profile geometry is bounded to 100,000 rows at 99 Hz. The producer
allows explicit bounded overrides up to 1,000,000 rows and 1,000 Hz, but those
numbers never become benchmark evidence.

Example command after PostgreSQL is running:

```bash
python3 benchmark/cpu_profile.py \
  --base-database-url "$DATABASE_URL" \
  --output "$RUNNER_TEMP/cpu-profile-artifacts" \
  --allocator jemalloc \
  --reader paging
```

CPU-profile elapsed time is attribution-only. On-CPU sampling can understate
database/network/lock/sleep waits, and hosted-runner profiles prove
orchestration/readability rather than stable-host absolute performance.

The first authoritative hosted ARM64 dispatch proved that Linux `perf` is
installed but unavailable to the unprivileged runner because
`perf_event_paranoid=4`. That failed capability probe remains retained
evidence; it is not a CPU profile.

For hosted runners, `benchmark/cpu_profile_gperftools.py` provides a separate
external sampling path using gperftools `libprofiler` with
`ITIMER_PROF/SIGPROF`. The workflow installs Ubuntu's packaged
`google-perftools` and `libgoogle-perftools-dev`, records their exact package
versions, and retains both the raw gperftools profile and the exact profiled
binary so the raw evidence can be re-symbolized later.

The fallback is allocator-safe by contract: before profiling, it resolves
`libprofiler.so`, records its SHA-256 and dynamic dependencies, and fails
closed if any dynamic dependency names tcmalloc. The workload's selected
system/jemalloc/mimalloc/snmalloc/tcmalloc allocator therefore cannot be
silently replaced merely to obtain CPU attribution.

The hosted workflow preserves both capability records:
- the original Linux `perf` probe, which may remain unsupported;
- the gperftools capability record used by the fallback producer.

## CPU attribution rehearsal status

The authoritative hosted ARM64 fallback dispatch completed successfully for
system+paging on exact OxideBatch 0.6.0. The retained bundle independently
validated the unavailable Linux perf capability, the allocator-safe
gperftools fallback, raw CPU profile, projections, exact profiled binary,
correctness verifier, and artifact hashes. The CPU attribution lane is
therefore rehearsed for Phase 0; its samples remain attribution-only evidence.

## Heap/allocation tool screening

For the generic heap lane, the first host/tool combination selected for
runtime qualification is Ubuntu 24.04 ARM64 plus the distribution
`heaptrack` package. Package availability alone is not treated as runtime
support.

The screening boundary is intentional:

- `heaptrack` is selected for the system allocator because it records
  allocation call stacks, allocation counts, peak-live contributors,
  temporary allocations, allocation-size histograms, Massif-compatible
  live-heap projections, and folded stacks while retaining a native trace;
- Valgrind Massif remains a fallback for cases where page-level allocation
  visibility is more important, but its substantially heavier instrumentation
  and earlier aggregation make it a poorer first Phase 0 allocation tracer;
- the gperftools heap profiler is not a generic fallback because it requires
  tcmalloc and would therefore replace the allocator under test;
- replacement Rust global allocators such as jemalloc/mimalloc/snmalloc/tcmalloc
  may bypass the malloc-family functions heaptrack interposes. Generic
  heaptrack evidence must therefore not be claimed for those allocators.
  Their memory state belongs in later allocator-native tooling unless a
  separate generic interception path is qualified.

## Heap/allocation attribution producer

`benchmark/heap_profile.py` is the initial heap-lane producer and is
fail-closed to `allocator=system`.

The producer:

- builds the exact published OxideBatch 0.6.0 workload in release mode with
  `CARGO_PROFILE_RELEASE_DEBUG=1` in an isolated Cargo target directory so
  allocation stacks can be symbolized without changing benchmark-lane builds;
- retains the exact profiled binary and records the material build flag;
- passes the PostgreSQL URL through `DATABASE_URL` for the profiled child so
  credentials are absent from the heaptrack-recorded argv;
- rejects a pre-existing `LD_PRELOAD`;
- records exact Ubuntu `heaptrack`/`libheaptrack` package versions,
  `libheaptrack_preload.so` path/SHA-256/dynamic dependencies, and fails
  closed if the preload library dynamically names a known replacement
  allocator;
- provides a DB-independent `--probe-output` mode that compiles a tiny
  debug-symbol C program with a known noinline malloc call site, profiles it,
  generates an allocation folded-stack projection, and accepts the host only
  when that exact known symbol is present in a readable stack; non-empty trace
  bytes and a non-empty report alone are not sufficient;
- records the probe compiler/version, compile status, binary size, trace size,
  folded-stack size/weights/readability, known-symbol observation, and bounded
  stderr/status evidence in capability schema
  `oxide-batch-heaptrack-capability/v2`;
- retains the native heaptrack trace as authoritative evidence;
- derives reviewable projections with `heaptrack_print`: the full allocation
  report, allocation-size histogram, allocation folded stacks, temporary
  allocation folded stacks, and Massif-compatible live-heap snapshots;
- emits a compact `heap/summary.json` v2 containing allocation count,
  cumulative allocated bytes, peak-live bytes, temporary-allocation count and
  ratio, histogram bucket count, largest allocation size, and allocation-stack
  readability/weight evidence;
- derives cumulative allocated bytes from the event-counted size histogram
  only when the histogram event count exactly matches heaptrack's global
  allocation count;
- derives exact peak-live bytes from the maximum `mem_heap_B` Massif
  snapshot;
- rejects a workload profile whose allocation folded-stack projection is empty,
  consists only of `??`/raw-address frames, or whose folded-stack total weight
  does not equal heaptrack's global allocation count;
- runs the unchanged independent workload verifier and emits a validated
  `lane=heap` manifest with `profiler_attached=true` and no
  `benchmark_timing`.

Default heap geometry is intentionally smaller than the benchmark lane:
25,000 rows, seed 266, chunk size 1000, and the normal reader-specific
fetch/page size. The producer caps this instrumentation lane at 100,000 rows.

Capability probe before starting PostgreSQL:

```bash
python3 benchmark/heap_profile.py \
  --probe-output "$RUNNER_TEMP/heaptrack-capability.json"
```

Example profile after PostgreSQL and heaptrack are available:

```bash
python3 benchmark/heap_profile.py \
  --base-database-url "$DATABASE_URL" \
  --output "$RUNNER_TEMP/heap-profile-artifacts" \
  --allocator system \
  --reader paging
```

The native heaptrack trace remains authoritative over the summary and all
generated projections. Heap-instrumented runtime is never benchmark evidence.

### Authoritative hosted ARM64 finding

The first authoritative main dispatch, run `43845055327` on
`b2749a7526569c2ab02a5283812bb29499e7bca9`, proved that heaptrack 1.5.0 on
the hosted ARM64 image can record reproducible scalar heap evidence while
failing the actual attribution requirement.

That retained run independently validated 739,985 allocations, 74,352,509
cumulative allocated bytes, 1,283,088 peak-live bytes, 276,130 temporary
allocations, the complete artifact hashes, and unchanged workload correctness.
However both folded-stack projections collapsed to `??`, the textual report
collapsed to `0x0 / in ??`, and all native allocation-info records referred to
trace index zero. The retained Rust binary was independently confirmed to
contain debug/unwind/symbol sections and was not stripped.

Therefore run 43845055327 is accepted as **scalar heap evidence** but explicitly
rejected as allocation-stack attribution evidence. The older v1 capability
probe was too weak because it only proved that heaptrack could produce a
non-empty trace/report.

The v2 capability contract fixes that false positive: a compiled known malloc
call site must survive recording and projection with its named symbol visible.
The workload producer independently enforces readable folded-stack attribution
as a second gate.

The hardened authoritative rerun, run `43858820727` on
`316b8d24b28283fa6e5d368af19d6d0b081ec01b`, then closed the host/tool
classification. Its purpose-built C probe compiled successfully, heaptrack
recorded a non-empty native trace, and `heaptrack_print` completed, but the
folded allocation stack still contained only unknown attribution. The known
symbol `oxide_batch_heap_probe_allocate` was absent. Therefore Ubuntu 24.04
hosted ARM64 plus the distribution heaptrack 1.5.0 package is **unsupported
for generic heap allocation-stack attribution** in this campaign. This does
not invalidate the scalar heap evidence retained from run 43845055327.

## Perfetto heapprofd fallback capability

The first allocator-preserving fallback selected for qualification is Perfetto
heapprofd host mode. This is a separate capability path from heaptrack and does
not weaken the heaptrack failure classification.

`benchmark/perfetto_heap_probe.py` is intentionally capability-only. It does
not run PostgreSQL, produce OxideBatch workload evidence, or make benchmark
timing claims.

The provenance contract follows the upstream release itself rather than a
floating download:

- Perfetto release: `v59.1`;
- release commit: `1e9c61e9844affd7dccd3d42d9ea9f3a3d10cf6d`;
- `tools/heap_profile` Git blob:
  `1d5b1cc8d9bd38749c6d200a8d5aac9b616b84ed`;
- the v59.1 script embeds prebuilts rolled at `v58.2`, so the campaign pins
  those exact upstream artifacts instead of pretending that they are v59.1
  binaries;
- linux-arm64 tracebox SHA-256:
  `2d21883e9c55818f851fd8c391afb9b366032b3110076b4fd41f31f30b5849fa`;
- linux-arm64 `libheapprofd_glibc_preload.so` SHA-256:
  `4ca10d32455973389d0f772f1c9ad8616b61eb46edc3b995a2825dee9fab2662`;
- linux-arm64 trace processor SHA-256:
  `0e6e0c5452c505c8d46fe472fd196a0d17d963460727e2ce2013b02aa1309555`.

The producer does not download any of those files. The manual
`.github/workflows/probe-perfetto-heap-capability.yml` workflow provisions
them from the pinned upstream URLs, records a provision manifest, and fails
closed unless the script Git-blob identity plus prebuilt byte sizes/SHA-256
values match exactly. The producer independently repeats the same identity
checks before any profiler execution.

The allocator boundary is also fail-closed. Perfetto documents the standalone
glibc preload interceptor as calling glibc-specific `__libc_malloc`-family
symbols so that the system allocator continues to serve the allocations. The
probe additionally records `ldd` output and rejects a preload binary that
dynamically names jemalloc, mimalloc, snmalloc, tcmalloc, or rpmalloc.

The runtime qualification uses another tiny debug-symbol C program with the
known noinline allocation function
`oxide_batch_perfetto_probe_allocate`. It runs host-mode heapprofd with:

- sampling interval `1`, Perfetto's fully accurate high-overhead mode;
- explicit pinned tracebox, preload, and trace processor paths;
- a required, version-recorded `llvm-symbolizer` on `PATH` for native
  symbolization;
- blocking-client mode so the bounded capability run fails by latency rather
  than silently losing allocation events;
- a retained raw Perfetto trace plus the generated gzipped pprof projections;
- `PERFETTO_BINARY_PATH` fixed to the probe-binary directory so the helper
  emits separate symbol packets and a `symbolized-trace`; the untouched
  `raw-trace` remains authoritative.

The symbolized trace is queried with the pinned trace processor when available;
otherwise the raw trace is queried and the known-symbol gate will fail closed.
Support requires:

- positive allocation count and bytes in `heap_profile_allocation`;
- at least one named frame reachable from a positive allocation callsite;
- the exact known probe symbol to appear in an allocation-reachable stack;
- at least one non-empty pprof projection;
- zero recorded heapprofd corruption, buffer-overrun, guardrail, client,
  malformed/missing-packet, non-finalized-profile, or sampling-interval-adjusted
  events;
- all pinned provenance and allocator-boundary checks to pass.

Capability schema is
`oxide-batch-perfetto-heap-capability/v1`. The output directory retains the
probe source/binary, helper stdout/stderr, raw trace, pprof projections, SQL
projections, hashes, and capability document. Sampling interval 1 is strictly
instrumentation evidence and must never be compared with benchmark-lane
timings.

### First hosted ARM64 Perfetto run: wrapper compatibility failure

Authoritative run `43869082342` on exact main
`16966581bc04b147c973f85ff799c4c40a6b314b` successfully provisioned every
pinned Perfetto input and compiled/executed the known C probe far enough to
produce a non-empty raw trace. The run did **not** classify heapprofd
unwinding support because post-processing failed first.

The retained evidence showed:

- all v59.1 script / v58.2 ARM64 helper identities matched exactly;
- the glibc preload retained only system-library dependencies and no known
  replacement allocator;
- the C probe compiled successfully;
- `raw-trace` was non-empty;
- the v59.1 `heap_profile` wrapper invoked its helper using the legacy
  `traceconv symbolize` / `traceconv profile` CLI;
- the pinned v58.2 helper is `trace_processor_shell`, whose supported
  interface is `util symbolize` and `convert profile --output-dir`;
- therefore symbol packets, pprof output, and SQL attribution checks were never
  reached.

This is classified as a **harness compatibility failure**, not a hosted-runner
permission failure and not a heapprofd unwind/symbolization failure.

The capability producer now generates and retains a narrow compatibility shim
that translates only the two legacy calls made by `heap_profile`:

- `symbolize TRACE` -> `trace_processor_shell util symbolize TRACE`;
- `profile [--no-annotations] TRACE` ->
  `trace_processor_shell convert profile [--no-annotations] --output-dir ... TRACE`.

The upstream trace processor binary remains byte-for-byte pinned and is still
used directly for all SQL evidence queries. The shim is campaign-owned,
retained with SHA-256 evidence, and covered by dependency-free CI tests.

### Second hosted ARM64 Perfetto run: missing llvm-symbolizer prerequisite

Authoritative run `43875632737` on exact main
`ccb8a69f62e8b85598e6e891f2a622a18fc27cc7` passed pinned provisioning and
used the retained traceconv compatibility shim, but it still did not reach a
valid unwind/symbolization classification.

Retained evidence showed:

- all pinned source/prebuilt identities matched exactly;
- allocator semantics were still system/glibc;
- the known C probe compiled successfully;
- heapprofd connected, collected, shut down cleanly, and retained a non-empty
  `raw-trace` (2,063 bytes);
- the compatibility shim was present and retained with its SHA-256;
- Perfetto then logged `Failed to exec llvm-symbolizer (errno: 2)`;
- no symbolized trace or pprof projection was produced;
- therefore allocation SQL / known-symbol gates were not reached.

Perfetto's own native-symbolization documentation requires
`llvm-symbolizer` on `PATH` and recommends installing the `llvm` package
on Debian/Ubuntu. This run is classified as an **environment prerequisite
failure**, not a heapprofd collection failure and not evidence that hosted ARM64
unwinding is unsupported.

The producer now records the exact observed `llvm-symbolizer` path/version and
fails closed before profiling when it is unavailable. The manual workflow
ensures the prerequisite is present (installing Ubuntu's `llvm` package when
necessary) and retains a separate `perfetto-symbolizer.json` document with
the install result, path, version, and availability verdict.

### Third hosted ARM64 Perfetto run: capability PASS

Authoritative run `43880787544` on exact main
`65dfb6ebdd87976d5a5ec7188b3cd19e0189e922` closed the generic
system-allocator capability qualification:

- workflow conclusion: SUCCESS;
- all pinned Perfetto source/prebuilt identities matched;
- allocator semantics remained system/glibc with no replacement allocator;
- `llvm-symbolizer` was available;
- profiler exit status was 0;
- raw trace: 2,061 bytes;
- symbol packets: 332 bytes;
- symbolized trace: 2,393 bytes;
- one non-empty pprof projection;
- 35 positive allocations / 139,602 positive allocation bytes across four
  allocation callsites;
- 37 allocation-reachable frame rows, eight named frame rows;
- exact known C allocation symbol
  `oxide_batch_perfetto_probe_allocate` survived in an allocation-reachable
  stack;
- heapprofd error counter: 0.

Therefore Ubuntu 24.04 hosted ARM64 plus the pinned Perfetto heapprofd host path
is **qualified for generic system-allocator allocation-stack attribution**.
This capability result replaces heaptrack as the selected generic heap
attribution path, but does not itself complete the heap lane because it used a
purpose-built C probe rather than the OxideBatch workload.

The earlier symbolizer evidence stored only the first output line
(`llvm-symbolizer`), which proves executable availability but not the LLVM
version. The workload producer tightens this by parsing the full
`llvm-symbolizer --version` output for the version token and retaining both
that version and the executable SHA-256.

### Perfetto-backed OxideBatch heap workload producer

`benchmark/perfetto_heap_profile.py` carries the capability contract into the
real `postgres-postgres` workload while remaining intentionally limited to
the `system` allocator.

It reuses the existing heap workload geometry and correctness path:

- default rows: 25,000;
- seed: 266;
- chunk size: 1,000;
- cursor fetch size: 500;
- paging page size: 750;
- release build with `CARGO_PROFILE_RELEASE_DEBUG=1`;
- exact locked OxideBatch `=0.6.0`;
- independent `postgres-postgres verify` correctness digest after the
  profiled run.
- Perfetto host mode does not propagate the child workload exit code, so the
  independent verifier digest is the authoritative workload completion and
  correctness gate.

The producer independently rechecks:

- Perfetto v59.1 `heap_profile` source identity;
- exact v58.2 ARM64 tracebox/preload/trace-processor sizes and SHA-256 values;
- glibc preload dynamic dependencies and absence of known replacement
  allocators;
- Linux aarch64 host scope;
- `llvm-symbolizer` availability, parsed version, and executable SHA-256.

The profiler command retains interval 1, blocking-client mode, explicit pinned
helper paths, and the campaign traceconv compatibility shim. The workload
database URL is supplied only through the child environment, not command-line
evidence.

The producer retains the authoritative raw trace plus:

- symbol packets;
- symbolized trace;
- pprof projection(s);
- positive allocation-count/byte/callsite SQL projection;
- heapprofd failure/data-loss SQL projection;
- complete allocation-reachable named-symbol projection;
- stack summary projection;
- profiler stdout/stderr;
- compatibility shim;
- profiled binary;
- correctness verifier;
- run/capability/summary metadata and hashes.

The workload attribution gate is deliberately stronger than the C capability
gate. A PASS requires at least one **single allocation callsite stack** that
contains all of:

- a readable Rust-style frame;
- a `postgres_postgres` frame;
- an `oxide_batch` frame.

It also requires non-empty raw/symbolized/symbol/pprof evidence, positive
allocation counters, zero heapprofd error counters, and correctness PASS.
Merely producing a trace or some readable symbols is insufficient.

The summary schema is
`oxide-batch-perfetto-heap-profile-summary/v1`. The campaign profile manifest
remains lane `heap`, carries no `benchmark_timing`, and marks all profiler
timing as attribution-only.

### Manual hosted ARM64 qualification workflow

`.github/workflows/probe-perfetto-heap-capability.yml` is manual-only and
runs on `ubuntu-24.04-arm`. It does not install Rust, start PostgreSQL, or run
the OxideBatch workload.

The workflow retains:
- exact repository SHA/run context;
- per-download status plus observed size/hash/executable-bit evidence;
- `llvm-symbolizer` install status, path, and exact observed version;
- the capability document and all producer-retained raw/projection artifacts;
- evidence even when provisioning or capability qualification fails.

Provisioning and capability execution use `continue-on-error` only so the
evidence can be uploaded. A final postcondition still fails the workflow when
provisioning, capability qualification, or retained-evidence validation did not
pass. Therefore a red workflow remains a meaningful unsupported/blocked
qualification artifact instead of losing the diagnostic state.

## Next implementation boundary

Review and merge the Perfetto-backed OxideBatch workload producer first. The
following separate unit may add a manual hosted-ARM64 workflow that provisions
the already-qualified pinned Perfetto inputs, starts PostgreSQL, and runs only
one bounded `system+paging` workload profile.

That runtime workflow must retain evidence even on failure and must not weaken
the cross-boundary Rust/OxideBatch stack gate. Only after a real workload run
passes correctness plus readable `postgres_postgres -> oxide_batch`
allocation attribution can the generic heap-attribution part of lane C be
closed. Replacement allocators remain Lane E; timeline/concurrency,
soak/quiescence, and allocator-native telemetry remain separate later units.
