# Modifiers

| Type | Effect |
|---|---|
| `OCamlRunParam` | appends a `key=value` to `OCAMLRUNPARAM`; `val: "s={0}"` templates the sweep value |
| `EnvVar` | sets an environment variable (`MMTK_PLAN`, `MMTK_THREADS`, ...) |
| `Wrapper` | prepends a command (`/usr/bin/time`, `olly gc-stats`, ...) |
| `ProgramArg` | appends arguments to the benchmark |
| `PerfAndOllyAttach` | attaches the counter tool **and** `olly gc-stats --json` to the running process |
| `MemtraceAttach` | enables [memtrace](https://github.com/janestreet/memtrace) allocation tracing (opt-in per benchmark, see below) |
| `CpuPin` | confines the benchmark to one hardware thread per physical core, and puts the observer tools on the complement |
| `ModifierSet` | a named bundle of other modifiers |

Shipped in the OCaml bases:

| Name | Type | Meaning |
|---|---|---|
| `s`, `o`, `a` | `OCamlRunParam` | minor heap words (default 262144), space overhead % (default 120), allocation policy. `micro_base` only; experiments that sweep them on macro declare them locally, along with `M` (`custom_major_ratio`). |
| `re`, `md` | `OCamlRunParam` | runtime-events ring size `e=` (log2 words per domain) and max domains `d=`. See [the ring](#sizing-the-runtime-events-ring). |
| `re_par`, `md_par`, `pin_lavyek` | ditto + `CpuPin` | the parallel-suite counterparts (macro only) |
| `pin_bench`, `pin_bench_mc` | `CpuPin` | one core for single-threaded benchmarks, the whole reserved set for self-pinning multi-domain ones (macro only) |
| `plan`, `threads` | `EnvVar` | `MMTK_PLAN` / `MMTK_THREADS` |
| `perf_grp1/2/3` | `PerfAndOllyAttach` | the three Linux counter groups below |
| `perf_grp1/2/3_freebsd` | `PerfAndOllyAttach` | their FreeBSD (`pmcstat`) counterparts. Event names differ and pmcstat allocates all-or-nothing, so never mix the two spellings. |
| `memtrace_grp1` | `MemtraceAttach` | allocation tracing; macro only, and scoped to `test_decompress` |
| `time_stats` | `Wrapper` | `/usr/bin/time ...` |
| `olly_gc` | `Wrapper` | `olly gc-stats` as a command prefix. `micro_base` only. |
| `gc_verbose`, `gc_verbose_oxcaml` | `OCamlRunParam` | GC stats at exit. **OxCaml reshuffled the verbosity bits**: stock OCaml wants `v=0x400`, OxCaml wants `v=0x1000`, and the wrong one silently prints nothing. `gc_verbose_oxcaml` is `micro_base` only. |

Modifiers can carry `excludes:` (a `suite → [programs]` map they should *not*
apply to) or `includes:` (the only programs they apply to). That is how one
pinning policy is routed to the single-threaded benchmarks and another to the
multi-domain ones.

## Sizing the runtime-events ring

`olly` reads GC events out of the runtime-events ring buffer. If the ring is too
small, events are lost (`[ring_id=N] Lost ... events`) and the derived statistics
degrade; in the worst case per-domain wall time falls back to `now - boot_time`
and you get a large negative `wall_time`.

- `e=` is log2 words **per domain**, OCaml's default is 16 (64K words).
- `d=` is max domains. The events file is `d * 2^e * 8` bytes, so raising `e` while
  leaving `d` at its default of 128 demands gigabytes and aborts the run.

In **micro** configs these are the `re` / `md` modifiers on the config string. In
**macro** they have moved into the suites: a suite or program declares
`ocamlrunparam: "e=25,d=2"` and it is merged over any config-string value, so the
five suites that actually overflow the ring carry their own size and nothing
global shadows them.

## Hardware counters

`perf_grp1/2/3` (Linux) and `perf_grp1/2/3_freebsd` (FreeBSD) collect hardware
counters and GC telemetry in the same run, as structured JSON. Three groups, sized
to fit the PMU without multiplexing: run one group at a time.

| Modifier | Counters | For |
|---|---|---|
| `perf_grp1` | task-clock, page-faults, cycles, instructions | baseline IPC |
| `perf_grp2` | task-clock, cycles, stalled-cycles-frontend/backend | pipeline stalls |
| `perf_grp3` | task-clock, cycles, cache-misses, LLC-load-misses, dTLB/iTLB-load-misses | cache and TLB |

Every backend **attaches** to a process the harness already owns, so the benchmark
stays a direct child whose exit status still distinguishes a crash from a clean
run. The FreeBSD groups are smaller (no usable `task-clock`, a hard ceiling of 7
counters), and `RUNNING_NG_COUNTER_BACKEND` forces a backend, which is how the
FreeBSD path is tested on Linux. The attach mechanics, and the full FreeBSD
status, are in [CLAUDE.md](../../CLAUDE.md) and
[freebsd-support.md](../freebsd-support.md).

## Allocation tracing

`memtrace_grp1` records **where a benchmark allocates**, sampled, as a
[memtrace](https://github.com/janestreet/memtrace) trace you can open in
`memtrace_viewer`. It answers a different question from the counters: not "how much
did the GC cost" but "which call stacks produced the garbage".

**It is opt-in per benchmark.** memtrace has no attach-to-a-running-process path,
so tracing starts only if the benchmark's own binary is linked against `memtrace`
and calls `Memtrace.trace_if_requested ()` at startup. The modifier only exports
`MEMTRACE` and `MEMTRACE_RATE`; the binary does the rest. In `macro-benches` only
`test_decompress` is patched this way today, so enabling the modifier elsewhere is
silently a no-op. The safety net is `runbms`'s warning when tracing was requested
and no trace appeared: trust that, not the `excludes:` map.

Per invocation you get a raw `.trace` (one process lifetime each, so unlike the
olly/perf sidecars these are per-invocation files) plus a `.json` folded-stack
summary, `{"stack": [...], "samples": N}` per aggregated call stack, for quick
diffs without opening the viewer. `val:` sets `MEMTRACE_RATE`, the proportion of
allocated words sampled; memtrace's own default of `1e-6` is very sparse, and
`val: "0.001"` gives roughly 950 times more samples and a few MB per second of
benchmark, so budget disk accordingly.

Try it with the shipped proof-of-concept, which runs `test_decompress` alone and
forces a rebuild so a stale pre-memtrace binary cannot silently skip tracing:

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/memtrace.yml \
LOG_DIR=/tmp/memtrace \
  bash run_ocaml_bench_gc_sweep.sh
```
