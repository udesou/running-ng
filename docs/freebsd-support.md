# OCaml support on FreeBSD

Status of running-ng's OCaml benchmarking on FreeBSD, and every way it differs
from Linux. Validated on `rosemary`: FreeBSD 15.1, Xeon E5-2640 v4
(Broadwell-EP), 20 physical / 40 logical cores, 2 NUMA nodes.

macOS is covered only as far as the `none` counter backend; see the last
section.

## What works

The full path runs end to end: opam switch provisioning, benchmark builds,
hardware counters, GC metrics, CPU pinning.

Coverage run, 196 micro benchmarks, one invocation each:

| suite | n | ran | counters | olly |
|---|---|---|---|---|
| sandmark-sequential | 118 | 118 | 118 | 118 |
| sandmark-with-packages | 12 | 12 | 12 | 12 |
| sandmark-with-deps | 5 | 5 | 5 | 5 |
| multicore-numerical | 22 | 22 | 22 | 22 |
| multicore-effects | 17 | 17 | 17 | 17 |
| multicore-structures | 7 | 7 | 7 | 7 |
| multicore-gcroots | 3 | 3 | 3 | 3 |
| multicore-grammatrix | 2 | 2 | 2 | 2 |
| multicore-simple-tests | 2 | 2 | 2 | 2 |
| multicore-minilight | 1 | 1 | 1 | 1 |
| mpl | 5 | 5 | 5 | 5 |
| graph500par | 1 | 1 | 1 | 1 |
| oxcaml-prefetch | 1 | 0 | 0 | 0 |
| **total** | **196** | **195** | **195** | **195** |

**No FreeBSD portability failure was found in any suite.** The two expected to
be trouble were clean: `with-deps` and `with-packages` both built and ran in
full, including everything pulling real opam packages (`zarith_*` via gmp,
`test_lwt`, `test_sched`, `graph500seq`, `knucleotide`). Note gmp was already
installed there; on a host without it, that is where failures would appear.

`oxcaml_prefetch` needs a `type: OxCaml` runtime, so under an OCaml runtime the
harness rejects it at build time with a precise reason and carries on. That is
correct behaviour, not a portability gap, and the suite is disabled in
`all_micro_freebsd.yml` so it does not recur as noise. OxCaml on FreeBSD is
untested and out of scope.

## Discrepancies from Linux

### 1. Counter tool, and one metric that does not exist

| | Linux | FreeBSD |
|---|---|---|
| tool | `perf stat --json --inherit -p PID` | `pmcstat -C -d -t PID -p EVENT` |
| arming | `--control` fifo handshake | wait for pmcstat's `#` header |
| descendants | `--inherit` | `-d` |
| privileges | some events need root | none, but `hwpmc` must be loaded |
| over-subscribe | multiplexes and time-scales | hard-fails; counts are always unscaled |
| bad event name | that event fails | **the whole group yields nothing**, exit 71 |

`task_clock` **is not available on FreeBSD** and is the only contract metric
missing. `CLOCK.STAT` is the nearest soft event but counts stat-clock ticks at
~127 Hz, so it would be a coarse estimate wearing the name of a Linux exact
one. olly's `cpu_time` covers what it was wanted for and is present on both.

`page_faults` **is** available, as the soft PMC `PAGE_FAULT.ALL`, aliased onto
perf's spelling so it reaches the same contract metric. Measured within 2.8% of
rusage on a GC-heavy workload. `PAGE_FAULT.READ` / `.WRITE` also work and have
no Linux equivalent; on GC workloads write faults are ~99.97% of the total.

### 2. Event names, and the counter ceiling

`task-clock`, `page-faults`, `cycles`, `branch-misses` and `cache-misses` do
**not** resolve as PMC names. Of the Linux groups' events, only `instructions`
does. So FreeBSD configs use `perf_grp{1,2,3}_freebsd`, never the Linux groups;
`tests/test_freebsd_event_groups.py` enforces that, and enforces that no event
name is used which has not been run on real FreeBSD hardware.

The ceiling is **7 events: 3 fixed-function plus 4 programmable** (four, not
eight, because SMT is enabled). `perf_grp3_freebsd` sits exactly at it. Soft
PMCs are a separate class with their own 16 rows, so `PAGE_FAULT.ALL` costs no
programmable counter.

Groups 2 and 3 are **approximations, not equivalents**. `resource_stalls.any`
and `idq_uops_not_delivered.core` stand in for perf's derived
`stalled-cycles-*`; `mem_load_uops_retired.l3_miss` counts retired load uops
where Linux's `LLC-load-misses` counts requests. Directionally useful within
FreeBSD, **not numerically comparable** to Linux figures. Group 1
(`instructions`, `cycles`, `page-faults`) is a genuine equivalent.

### 3. Pinning

Mechanism differs (`cpuset -l` versus `taskset -c`) and so does the CPU list
for the same policy: Linux enumerates SMT siblings as `(0,16),(1,17)…`, FreeBSD
as `(0,1),(2,3)…`, so "one thread per physical core" is `0-15` on one and
`0,2,…,30` on the other. Hence `CpuPin` derives the list from the running
machine rather than taking it from a config.

### 4. pmcstat's intermediate output is stale, not sampled

hwpmc saves a process-scope counter when the target is switched out, so reading
it mid-run returns the last saved value. Only the final row, written at exit, is
the true total. A killed or never-confirmed pmcstat therefore leaves a
plausible-looking undercount, so those results are discarded rather than
published. perf has no equivalent hazard.

### 5. Platform mechanics

- No bash in the base system, so `install_deps_freebsd.sh` and
  `scripts/portability_probe.sh` are POSIX `sh`. The sweep wrapper is bash and
  is found via `#!/usr/bin/env bash` at `/usr/local/bin/bash`, so it must be
  invoked as `bash`, not `sh`.
- `/bin/true` is `/usr/bin/true`; python3 lives in `/usr/local/bin`.
- `install_deps_freebsd.sh` assumes **no root**: opam goes in as a user-local
  static binary and sandboxing is off (bubblewrap is Linux-only). `--check`
  reports what needs an administrator instead of failing halfway.
- Needs root once, for `kldload hwpmc`. After that, process-scope PMCs need only
  `p_candebug(9)`, and `security.bsd.unprivileged_proc_debug` defaults to 1.

## Not a discrepancy: things that look like one

- **`page-faults` in the sidecar versus `page_faults` in the contract.** Two
  alias hops, both intended: `counters.py` maps the tool's spelling to perf's,
  then `contract/vocab.py` maps perf's to the canonical metric.
- **A larger runtime_events ring is needed for GC-dense benchmarks.** Four
  benchmarks (the globroots trio and `pidigits5`) overflow the default ring and
  olly marks its own output `stats_reliable: false`. This is **not
  FreeBSD-specific**: every established micro config carries `re-25|md-2` and
  the first FreeBSD coverage config simply omitted it. It now carries it, and
  the harness warns whenever olly reports unreliable stats.
- **`page-faults` disagreeing with rusage on a short benchmark.** Faults taken
  before the PMC attaches are a large fraction of a short run: ratio 0.47 on
  `almabench` against 0.97-0.99 on a GC-heavy workload. Expected, not a defect.

## Known gaps

- **Macro benchmarks are set up but not yet run.** `~/macro-benches`'s
  vendoring scripts were GNU-only (20 `sed -i`, six of them GNU-only sed
  constructs, five `md5sum`, one `nproc`) and are now portable via
  `scripts/lib-portable.sh` there. `smoke_macro_freebsd.yml` and
  `all_macro_freebsd_tier1.yml` are ready. Tier 1 is the 57 benchmarks across
  13 suites needing no system library; the other eight suites need
  `pkg install` (`apron`/`camlidl` for goblint, `openblas` for owl, `gsl` and
  `sqlite3` for pplacer, `libevent` and friends for devkit, `zlib` for
  several), which needs root. Nothing has run on FreeBSD yet, and the likely
  failures are C stubs and configure scripts inside `duniverse/` rather than
  anything in running-ng.
- **lavyek cannot be covered anywhere.** It is in a private repo and disabled
  in the base config for everyone, so the parallel macro path and the
  `re_par`/`md_par`/`pin_lavyek` routing are untested on FreeBSD by
  construction, not by omission.
- **Multi-invocation runs are unmeasured.** Everything so far is
  `invocations: 1`, i.e. coverage rather than measurement. Run-to-run variance
  on this host is unknown, though 0.11% dispersion was seen on a fixed workload.
- **No config exercises pinning.** `CpuPin` is verified directly (benchmark
  `0,2,…,18`, observers `20-39`, siblings idle) but neither FreeBSD config
  carries it; add `|pin_bench` to a config string to cover it.
- **`stats_reliable` is not a contract metric.** The harness now warns, but a
  consumer reading the contract cannot filter unreliable rows. Fixing that
  needs a `bench-contract` vocabulary bump, since `vocab.py` is generated.
- **OxCaml on FreeBSD is untested**, deliberately out of scope.

## macOS

Runs on the `none` counter backend: olly and rusage work, hardware counters do
not. mperf (`github.com/tmcgilchrist/mperf`) is the candidate but needs root for
every invocation, because `kpc_force_all_ctrs_set` claims the PMU globally, and
it launches rather than attaches. macOS also has no API binding a process to a
core, so `CpuPin` contributes nothing there by design.
