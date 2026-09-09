# CLAUDE.md — working notes for agents & contributors on `running-ng`

Auto-loaded context for Claude Code (and orientation for contributors).
`README.md` is the human-facing overview: what the tool is, how to run it, how
to add a benchmark or an experiment. This file holds what doesn't belong there —
the internals you need when something breaks, the exact contracts, the
hard-won gotchas, and the current list of known-broken files.

## What this is

- A Python benchmark-orchestration framework (fork of `running-ng`, upstream
  `anupli/running-ng`) used here to evaluate **OCaml** compiler/GC performance:
  it builds compilers (via `opam-compiler`), builds benchmark binaries per
  runtime, runs them under `perf` + `olly`, sweeps GC parameters, and emits
  data-contract artifacts.
- Benchmarks live in sibling repos: micro = `~/benches` (13 suites, 195 enabled
  programs — 196 listed, `oxcaml_prefetch` needs an OxCaml runtime), macro =
  `~/macro-benches` (22 suites, 31 enabled programs; merlin and lavyek
  disabled). Both are driven through `OCamlBenchmarkSuite`.
  Both repos also carry their own program list (`manifest.yml` /
  `benchmarks/manifest.yml`) with `args` copied verbatim from the matching base
  config here, so they can build and run themselves without this repo.
  `~/benches/scripts/ci-manifest.py check --running-ng` diffs its manifest
  against `micro_base.yml` program-for-program and argument-for-argument — run it
  after touching `micro_base.yml`, since a program in only one of the two is
  either a benchmark that silently never runs or a sweep entry that fails every
  time.
- Entry points: `run_ocaml_bench_gc_sweep.sh` (build **+ run**) and
  `build_ocaml_binaries_gc_sweep.sh` (build **only**) → both find/create a tools
  switch (dune/ocamlfind), build/verify olly, put both on `PATH`, then call
  `python3 -m running <cmd> …`.
- Consumers: `~/ocaml-bench-dashboard` (owns the data contract + ingestor +
  dashboard), `notebooks/`, `scripts/plot_gc_sweep.py`.

## Hard rules (do not violate)

- **Do not comment on PRs, or add to PRs, unless explicitly asked to.**
- **No "Claude"/Anthropic/`Co-Authored-By: Claude` in commit messages.**
- **Remote is `origin = github.com/udesou/running-ng`** (a personal fork).
  Default working branch is **`adding-ocaml-support`** (all the OCaml support;
  it is also `origin/HEAD`); `master` is the upstream JVM/DaCapo lineage.
  Commit/push only when asked.
- **Don't commit `gc-sweep-logs*/`, `*-logs/`, or other run output** — huge and
  timestamped.
- **Config layering is law:** an experiment `includes:` a base
  (`base/ocaml/{micro_base,macro_base}.yml`) and declares only `runtimes`,
  `configs`, `modifiers`, `config_sweep`, `comparisons`, and `overrides`. See
  "Config merge" below — getting it wrong is the #1 config bug.
- Keep docs consistent with every commit: `README.md`, this file, and the
  header comment of any config you touch.

## Branch state (read before editing docs)

`adding-ocaml-support` is the trunk of this fork. As of 2026-07-31 it carries
the data contract (PR #5), reproducible switch provisioning (PR #4) and memtrace
support (PR #3) — all three merged, so the contract, `adapt`, `MemtraceAttach`
and the opam-root lock documented here are all present on it.

Still unmerged:

- **`knob-a-rungs`** — the input-size ladders: `<tool>_..._{small,default,large}`
  rungs in `macro_base.yml`, the size/legacy tags, and one `*_ladder_5.5.0.yml`
  olly-pass config per tool. Independent of the contract work; the two touch
  `macro_base.yml` in different places. (Branch name keeps the historical
  "knob-a" label; the docs/configs call it the input-size ladder.)
- **`mmtk-minheap`** — `experiments/mmtk_minheap.yml` + its result file (see the
  known-broken table).

## Input-size ladder + tags (macro, `knob-a-rungs`)

Each macro tool has a `{small,default,large}` (a few also `huge`) input-size
ladder — rungs chosen so each reaches a different GC/runtime regime, not just a
scaled-up copy of the one below. `macro_base.yml` enables **every** program in
`benchmarks:` (all rungs + legacy = 92), and `tags:` carries the run selectors:

- `default_run` / `small_run` / `large_run` / `huge_run` — the rung of that size
  across every tool (`default_run` = 20, one per tool; `huge_run` = 2).
- `legacy` (30) — the pre-ladder benches kept but not run by default: original
  anchors, extra per-tool workloads (cpdf ops, alt-ergo problems, menhir
  grammars, devkit stre/network/gzip), and the frozen issue reproducers
  (`liq_video_frames_pool` #14533, `goblint` #13733).
- `all_benches` (92) — everything runnable at once.

**A bare run (no `RUNNING_TAG`) auto-applies `default_run`** (`runbms.py` — guarded
on the tag existing, so micro-benches is unaffected). So the standard suite is the
default rungs; other sizes / legacy / everything are opt-in via `RUNNING_TAG`. The
tag filter is *intersection-only* (can't re-enable a program absent from
`benchmarks:`), which is why `benchmarks:` lists everything. Per-tool
`*_ladder_5.5.0.yml` configs still exist for one-tool olly passes (they override
`benchmarks:` to that tool's rungs; a bare run of one gives its `_default` rung —
pass `RUNNING_TAG=small_run,default_run,large_run` for all three).

## Where things live (read first)

- `src/running/command/` — subcommands registered in `__main__.MODULES`:
  `fillin`, `runbms`, `buildbms`, `minheap`, `log_preprocessor`, `adapt`.
  (`genadvice.py` exists but is **not** registered — dead code, not reachable
  as a subcommand.)
- `src/running/__main__.py` — besides dispatch, owns two run-scoped concerns:
  it reports `OpamRootBusyError` as a plain message + `exit(1)` rather than a
  traceback, and its `finally` calls `OCaml.restore_active_switch()` +
  `OCaml.release_opam_lock()` so an interrupted run still puts the user's opam
  switch back.
- `src/running/`
  - `runtime.py` — `OCaml` / `OxCaml` / `OCamlMMTk` / `NativeExecutable` + the
    JVM/JS lineage; opam-compiler switch management, satellite switches.
  - `benchmark.py` — `OCamlBuiltBinaryBenchmark` (build contract, binary
    caching, `.build-failed` sentinel) and the `PerfAndOllyAttach` run path.
  - `suite.py` — `OCamlBenchmarkSuite`, `OCamlMulticoreBenchmarkSuite`,
    `OCamlOxcamlBenchmarkSuite`, `OCamlMacroBenchmarkSuite`.
  - `modifier.py` — `OCamlRunParam`, `EnvVar`, `Wrapper`, `ProgramArg`,
    `PerfAndOllyAttach`, `MemtraceAttach`, `ModifierSet`, `Companion`,
    `CpuPin`.
  - `osinfo.py` — the OS abstraction: PID-to-executable lookup, host probes
    for the log prologue, CPU/SMT/NUMA topology, and the pinning command.
    Everything degrades rather than raising, because it all runs on the
    measurement path.
  - `counters.py` — hardware counter backends (`linux-perf`, `freebsd-pmc`,
    `none`) behind one interface.
  - `config.py` — includes/overrides merge, `validate()`, `validate_tags()`,
    `apply_tag_filter()`.
  - `contract/` — native contract emission (`native.py` orchestrates,
    `emit.py` normalizes, `vocab.py` is generated from the OCaml contract).
  - `analysis/json_sidecars.py` — sidecar discovery/parsing (new per-tool form
    plus the old combined form).
- `src/running/config/` — `base/ocaml/{micro_base,macro_base}.yml`,
  `examples/` (smoke tests), `experiments/` (one file per lab run, plus a few
  `*.md` findings write-ups).
- `contract-adapter/` — OCaml legacy→contract adapter + `gen_contract_py.py`
  (regenerates `src/running/contract/vocab.py` from the contract).
- `experiments/memtrace-poc/` — a standalone `Makefile` + `summarize_json.py`
  for poking at memtrace output; separate from the shipped
  `config/experiments/memtrace_poc.yml`.
- `run_ocaml_bench_gc_sweep.sh`, `build_ocaml_binaries_gc_sweep.sh`,
  `install_deps{,_linux,_macos,_freebsd}.sh`, `scripts/plot_gc_sweep.py`,
  `scripts/portability_probe.sh`, `notebooks/`.
- `docs/` — upstream's mdBook (`docs/src/`, JVM-oriented) plus this fork's
  methodology notes (`benchmark-calibration-triage.md`,
  `benchmark-noise-and-comparison-plan.md`, `benchmark-coverage-gaps-plan.md`).

## Build / run

- Env vars: see the README table. `RUNNING_BENCH_DIR` and
  `RUNNING_MACRO_BENCH_DIR` are **synonyms**, and **both** launch scripts now
  export both, with a lazy `../benches` fallback that does not require the
  directory to exist. Until 2026-08-10 only `run_ocaml_bench_gc_sweep.sh` did
  that; `build_ocaml_binaries_gc_sweep.sh` read `RUNNING_BENCH_DIR` alone and
  resolved its fallback **eagerly** under `set -e`, so a macro-only *build* that
  set only `RUNNING_MACRO_BENCH_DIR` aborted when `~/benches` was absent — and
  when it was present, the macro config's `${RUNNING_MACRO_BENCH_DIR}` reached
  the YAML unset. Keep the two blocks identical. The expansion is still the
  trap: `${…}` in a config is read at YAML-load time, so an unset variable gives
  you paths starting with its literal name rather than an error.
- Commands (`python3 -m running`, or the launch scripts):
  - `runbms LOG_DIR CONFIG` — build (skips if the output binary exists) then run.
    Extra args after the script name are forwarded (`-i`, `--resume`, `-d`, …).
  - `buildbms CONFIG` — build only. Prints a per-benchmark OK/FAILED table.
  - `minheap CONFIG RESULT.yml [-a N]` — per-(benchmark,config) binary search
    for the smallest heap. Needs binaries built; writes RESULT incrementally
    (**resumable** — skips benches already recorded).
- Runtimes: `OCaml` (`version:` | `commit:`/`hash:` | `executable:`), `OxCaml`,
  `OCamlMMTk`. Each non-`executable` runtime gets an opam switch
  `running-ng-<runtime-name>` built by `opam compiler create`; the switch is
  the cache, so delete the switch (not a temp dir) to force a compiler rebuild.
  Only **OxCaml** uses `/tmp/running-ng-ocaml-toolchains/` (for source
  checkouts).
  `version:`/`commit:` both resolve to a **git ref** — `version: "5.5.0"` →
  `opam compiler create ocaml/ocaml:5.5.0`, i.e. built from the release tag, not
  the `ocaml-base-compiler` opam package. That distinction matters: it is what
  keeps a runtime switch immune to whatever a shadowing opam repo happens to
  publish under the same version number.

### Switch provisioning (`OCaml._ensure_switch` and friends)

- **A switch left over from an earlier run is deleted and rebuilt**, because
  nothing in a switch records which compiler source or dune version built it.
  `RUNNING_REUSE_SWITCHES=1` restores the old reuse-if-present behaviour (worth
  it for long sweeps — a rebuild recompiles the compiler, ~10–20 min per
  runtime). Switches provisioned earlier in the *same* run are always reused
  (`_switches_created_this_run`).
- **Reuse mode refuses a half-built switch** (`_assert_switch_usable`). opam
  registers a switch name *before* its compiler finishes building, so an
  interrupted provisioning leaves the name present with no `bin/ocamlc` behind
  it. A normal run heals that by rebuilding; reuse mode can't — it holds only a
  **shared** lock and must not delete a switch a concurrent run may be using —
  so it raises with the two ways out (rerun without `RUNNING_REUSE_SWITCHES`, or
  `opam switch remove`). Without the check the empty shell reached the build
  scripts and surfaced as a benchmark build failure, nowhere near the cause.
  The check is compiler-only on purpose: `OCamlMMTk` shares this code path and
  deliberately installs no dune.
- **`OCaml.DUNE_VERSION` (3.24.0) is pinned** so switches provisioned months
  apart — and two switches compared within one run — get the same build tool,
  and a **failure to install it is fatal**. It used to be 3.22.1 with a warn-and
  -fall-back-to-`PATH` path, which quietly defeated the pin: 3.22.1 can't
  bootstrap against 5.6 trunk, so a trunk switch got no dune and silently used
  the tools switch's (installed *unconstrained*) — a 5.5.0-vs-trunk run built its
  two sides with different dune versions. If a compiler needs a different dune,
  say so with `dune_version:` on that runtime. **`OxCaml` inherits this path**
  (`OCamlMMTk` does not — it deliberately installs no dune and uses the tools
  switch), so an OxCaml runtime that can't take the pinned dune now needs an
  explicit `dune_version:`. Before raising the pin: build the *whole* suite on
  the candidate, confirm it bootstraps on **trunk** and not just the release, and
  note that an already-populated `macro-benches/duniverse/` keeps its old
  `dune-project` until `make setup` is re-run.
- **The opam root is locked** (`$OPAMROOT/running-ng.lock`, `flock`): exclusive
  for a normal run, shared under `RUNNING_REUSE_SWITCHES=1`, skipped for dry
  runs. A second run that would delete switches the first is using is refused
  with `OpamRootBusyError` rather than allowed to corrupt both. The kernel drops
  the lock on process exit, so a crash never wedges it. Two campaigns at once →
  give each its own `OPAMROOT`.
- The overlay repo is **opt-in** (`relocatable: true`) and scoped to one switch.
  It used to be added to every switch with `--set-default`; see the gotcha below.
- `configure_args:` is honoured (passed as `--configure-command "./configure …"`).
  `make_targets:` is **not implemented** — don't put it in a config.

### Benchmark build contract

`OCamlBuiltBinaryBenchmark._run_build` activates the runtime's switch env,
overlays `build_env:` then `runtime.get_build_env_overrides()`, and runs the
build script with `cwd = benchmark_dir`, prefixed by
`runtime.get_command_prefix()` (`setarch <arch> -R` for MMTk, empty otherwise):

| Variable | Value |
|---|---|
| `RUNNING_OCAML_BENCH_DIR` | the benchmark's `path:` |
| `RUNNING_OCAML_OUTPUT` | `binary:` (with `{benchmark}`/`{runtime}` expanded) else `<benchmark>-<runtime>` under `path:` |
| `RUNNING_OCAML_RUNTIME_NAME` | the runtime's config-file name |
| `RUNNING_OCAML_SWITCH` | switch name, when not in `executable:` mode |

Post-conditions and caching:
- The script **must** create `RUNNING_OCAML_OUTPUT`, or the build is an error.
- An existing output binary means **skip the build** (warning:
  `already exists; skipping build`) unless `always_build: true`.
- A failed build touches `<output>.build-failed` and **subsequent runs refuse
  to retry** until the sentinel is deleted.
- In-memory binary cache is keyed on `runtime.get_cache_key()`, which includes
  the runtime's *config name* — so `ocaml-5.4.1` and `ocaml-5.4.1-flambda`
  never share a cache entry despite the same `version:`.

### Output naming

`<bm>.<hfac|0>.<size|0>.<config with "|"→".">.<suite>.log`, sidecars
`olly_<same base>.json` / `perf_<same base>.json`, `.gz` when
`compress_logs` is true (its default when unset is **true**; both OCaml bases
set it to `false`). Also written into the run dir: `runbms.yml` (merged config,
post-`RUNNING_TAG`) and `runbms_args.yml`.

memtrace adds `memtrace_<same base>.<invocation>.trace` (raw) and
`memtrace_<same base>.<invocation>.json` (folded-stack summary). These are
**per invocation**, not per cell, because a trace covers one process lifetime —
so unlike the olly/perf sidecars they are not appended to.

## Platform support (Linux, FreeBSD, macOS)

The goal is "the same harness runs everywhere and emits contract-conformant
data", NOT numbers comparable across operating systems. Allocators, page
policy and schedulers differ too much for the latter to mean anything.

### Counter backends (`counters.py`)

| backend | tool | notes |
|---|---|---|
| `linux-perf` | `perf stat --json --inherit -p PID` | arms via the `--control` fifo handshake |
| `freebsd-pmc` | `pmcstat -C -d -t PID -p EVENT` | arms by waiting for pmcstat's `#` header |
| `none` | nothing | supported configuration, not a failure; olly and rusage still run |

All three ATTACH to a PID the harness already owns. This is deliberate and
should not be "simplified" into letting the tool launch the benchmark:
attaching is what keeps the benchmark our own direct child, so its exit status
still tells a crash from a clean run and a timeout can still kill it.
FreeBSD's `pmc stat` would have been a far easier parse and always returns 0
(`cmd_pmc_stat.c:481`), so every SIGSEGV would have read as a good
measurement.

Backends normalise to `{event, counter-value}`, stored under `"perf"` for
backward compatibility, with `counter_backend` recording which tool ran.
`RUNNING_NG_COUNTER_BACKEND` forces one, which is how the FreeBSD path is
exercised on Linux (see `tests/fixtures/fake_pmcstat.py`).

### CPU pinning (`CpuPin`, `osinfo.partition_cpus`)

The pinning MECHANISM is per-OS (`taskset -c`, `cpuset -l`, nothing on
macOS). The CPU LIST cannot be: on one Ryzen 9 9950X, Linux enumerates SMT
siblings as (0,16),(1,17)... so one-thread-per-core is `0-15`, while FreeBSD
on the same silicon enumerates (0,1),(2,3)... so it is `0,2,...,30`. So the
list is derived from the running machine, never written into a config.

`CpuPin` options:

- `val` (default `0`): whole physical cores handed to the observers instead
  of the benchmark. Raising it takes cores from the benchmark and so changes
  what is being measured. Not a mid-sweep decision.
- `one_node` (default false): confine the benchmark to one NUMA node and give
  the others to the observers. Exactly a no-op on a single-node machine, so it
  is safe to leave on in a shared config. `pin_lavyek` sets it.

**Observer placement.** olly and the counter tool are pinned to the complement
of the benchmark's set. olly is the one that matters: `perf stat` in counting
mode only reads counters at the ends, while olly continuously drains the
runtime_events ring alongside the benchmark.

Under `one_node` the benchmark's own SMT siblings are left IDLE rather than
given to the observers. With a whole spare node available the siblings buy
nothing and would contend for the same physical cores, so `one_node` genuinely
means no SMT contention. Without it, the siblings are the only spare CPUs
there are, so they go to the observers and the isolation is weaker than it
looks. Decided 2026-09-09 after measuring on a 2-socket Xeon that every one of
the benchmark's 10 cores had its sibling in the observer set.

Note `cpuset -l` sets CPU affinity but not the NUMA memory domain, so observer
allocations can still land on either node. `cpuset -n` is the knob if that
ever matters.

### FreeBSD specifics

- **PMCs need no root**, but `hwpmc` must be loaded (`kldload hwpmc`).
  `hwpmc(4)` requires root only for system-scope PMCs; process-scope ones need
  `p_candebug(9)`, and `security.bsd.unprivileged_proc_debug` defaults to 1.
- **Event names differ and are all-or-nothing.** `task-clock`, `cycles`,
  `branch-misses` and `cache-misses` do NOT resolve; one bad name
  makes pmcstat exit 71 and write nothing, so the whole group yields no
  counters. Use the `perf_grp*_freebsd` modifiers, never the Linux ones.
  `pmc list-events` (not `pmc list`) enumerates what a CPU supports.
- **Page faults come from a SOFT PMC**, `PAGE_FAULT.ALL` (`pmc.soft(3)`),
  aliased onto perf's `page-faults`. Soft PMCs are a separate hwpmc class with
  their own 16 rows, so they cost no programmable counter. `PAGE_FAULT.READ`
  and `PAGE_FAULT.WRITE` also exist and have no Linux equivalent. `task-clock`
  has no usable equivalent (`CLOCK.STAT` is ~127 Hz), so a FreeBSD run is
  missing that one contract metric and no other; olly's `cpu_time` covers what
  it was wanted for.
- **Counter ceiling is 7**: 3 fixed-function plus 4 programmable (four, not
  eight, because SMT is on). `perf_grp3_freebsd` is exactly at that ceiling.
  hwpmc never multiplexes, so it hard-fails where perf would silently
  time-slice and scale.
- **pmcstat's intermediate rows are stale, not sampled.** hwpmc saves a
  process-scope counter when the target is switched out, so only the final row
  written at exit is the true total. This is why a killed or unready pmcstat
  has its output discarded rather than parsed.
- **`install_deps_freebsd.sh` assumes no root.** opam goes in as a user-local
  static binary, sandboxing is off (bubblewrap is Linux-only), and `--check`
  reports what needs an administrator instead of failing halfway.
- No bash in the base system, so the FreeBSD script and the probe are POSIX
  `sh`. `/bin/true` is `/usr/bin/true`; python3 is in `/usr/local/bin`.

### macOS

Runs on the `none` backend. There is no counter tool wired up: mperf
(github.com/tmcgilchrist/mperf) is the candidate, but it requires root for
every invocation (`kpc_force_all_ctrs_set` claims the PMU globally, which no
entitlement or fork arrangement avoids) and it launches rather than attaches.
macOS also has no API that binds a process to a core, so `pin_command` returns
`[]` and `CpuPin` contributes nothing there by design.

## Gotchas (hard-won — don't rediscover)

- **Config merge.** Including a base then redefining one of its **top-level
  scalars** (`invocations`, `schema_version`, `compress_logs`, `remote_host`,
  `minheap_multiplier`, `heap_range`, `spread_factor`) at top level →
  `combine()` `TypeError`. Change them through `overrides:`. A top-level
  `benchmarks:` dict *merges/extends* the base's set rather than replacing it,
  so narrowing the suite must also go through `overrides:`.
- **`validate()` is strict about runtimes.** Declared-but-unused and
  compared-but-not-run are **errors**, not warnings. Comment out spare runtime
  declarations rather than leaving them in.
- **olly JSON sidecars are JSONL** — one line per invocation; don't infer
  invocation count from filenames.
- **Constructing an `OCaml` runtime provisions its opam switch.** `__init__`
  calls `_ensure_switch` eagerly, so `Configuration.resolve_class()` on a
  config that declares runtimes will create switches, and will WIPE and
  rebuild an existing one of the same name unless `RUNNING_REUSE_SWITCHES` is
  set. Parse YAML directly (`yaml.safe_load`) if you only want to inspect a
  config.
- **`opam compiler create` rejects a bare version.** `opam compiler create
  5.4.1` fails with `Invalid source: "5.4.1"`. It wants a source spec, which
  is what `runtime.py`'s `_opam_compiler_source` builds: `ocaml/ocaml:5.4.1`.
  The harness is right; only hand-written probe commands get this wrong.
- **The sweep wrapper no longer provisions anything.** It verifies the
  opam-compiler plugin and olly, then points at `install_deps_<os>.sh`. It
  used to install both into whichever switch it picked, which since the
  two-switch split could corrupt either one: opam-compiler downgrades the olly
  switch's cmdliner, olly's deps evict opam-compiler from the tools switch.
- **Don't add a FreeBSD PMC event name you have not run on FreeBSD.** pmcstat
  allocates all-or-nothing, so one unresolvable name silently costs the whole
  group its counters. `tests/test_freebsd_event_groups.py` pins the verified
  set for this reason.
- **Sidecars are per tool now** (`olly_*.json`, `perf_*.json`). The old single
  combined `<base>.json` is still *read* by `analysis/json_sidecars.py` for
  backward compat, but nothing writes it. The combined object is still embedded
  in the `.log` after `*****`.
- **Empty olly/perf output across a run** → check `/tmp` free space first (a
  tmpdir leak filled the tmpfs; the per-invocation runtime_events ring then
  SIGBUSes). `_run_with_perf_and_olly` now warns below 1 GiB free and always
  removes its tmpdir.
- **macro-lavyek** suites need `|re_par-22|md_par-8|pin_lavyek` in the config
  string, else olly drops events and `wall_time` goes negative (≈ −4.7M s = the
  system uptime, because per-domain wall time falls back to `now - boot_time`).
  The `_par` modifiers **require explicit values** — the bare token `re_par`
  renders the literal `e={0}` into `OCAMLRUNPARAM`.
- **`ocamlrunparam:` (per-benchmark ring/domains, replaces global `re`/`md`).** A
  suite field — set on a suite (default for all its programs) or a single program
  (overrides the suite) — merged over the config-string `re`/`md` key-by-key with
  the benchmark's value winning (`Benchmark.attach_modifiers`). This let the macro
  `re-25|md-2` move **out of the config strings** and into the benchmarks that need
  it, so no global value shadows another. A default-ring probe of every heavy rung
  found most tools lose **no** olly events at the default runtime_events ring — they
  carry nothing. Five suites overflow it and declare **`e=25,d=2`** at the suite
  level (`macro-{zarith,menhir-monorepo,eio,coq-monorepo,decompress}`; zarith drops
  ~290M events at default). The `d=2` is **required**, not just a domain cap: OCaml
  sizes the ring as `max_domains * 2^e`, so `e=25` alone at the default
  `max_domains=128` demands ~4GB and **aborts** (SIGABRT + "olly internal error") —
  `d=2` bounds it (~64MB). `e=25,d=2` is exactly the retired global `re-25|md-2`.
  `infer_*` instead set `"e=18,d=128"` — `--multicore` needs 12+ domains, so it keeps
  the ring small with a smaller `e` (128 * 2^18 ≈ 256MB) rather than a small `d`.
  Configs now use a bare `perf_grp1` (lavyek still adds `re_par|md_par|pin_lavyek`);
  one specialised lab config still carries `re-25` but the suite values override it.
  Values are 5.5.0/32-core minimums — re-probe on a very different farm.
- **`-d` (dry run) still provisions compilers.** `_ensure_switch` is called from
  `OCaml.__init__`, which `Configuration.resolve_class()` runs before any
  dry-run check — so `-d` on a config with an unbuilt runtime compiles a
  compiler from source before printing anything. It *is* honoured for contract
  emission (`schema_version and not is_dry_run()`) and for the opam lock. Use a
  config whose switches already exist if you only want to expand the grid.
- **A benchmark whose success is a non-zero exit needs `expected_exit:`.** The
  runner classifies any `returncode != expected_exit` (default 0) as
  `SubprocessrExit.Error`, and `contract/native.py` drops crashed invocations
  wholesale — so without the declaration the cell's olly/perf data is silently
  absent from `contract/`, while the legacy adapter (which reads the sidecars)
  keeps it. That divergence is invisible unless you diff the two paths. Only
  `alt_ergo_unsat_smt2` needs it today (142 = 128+14, its own SIGVTALRM from
  `--timelimit 15`); the field name and exact-equality semantics match
  macro-benches' `benchmarks/manifest.yml`, so keep the two in sync.
- **memtrace is opt-in per benchmark, and silently so.** `MemtraceAttach` only
  exports `MEMTRACE`/`MEMTRACE_RATE`; tracing happens only if the binary itself
  calls `Memtrace.trace_if_requested ()` (macro-benches patches this into
  `test_decompress` alone). `Modifier.excludes` is exclusion-only — there is no
  allowlist — so `memtrace_grp1` carries an `excludes:` map naming every *other*
  program, which means adding a benchmark to a **new** suite silently escapes the
  list. The real safety net is `runbms`'s warning when tracing was requested and
  no trace file appeared; trust that, not the excludes map. Sampling: memtrace's
  own default is `1e-6`, so `val:` is a multiplier on a very sparse baseline
  (`0.001` ≈ 950× more samples, 6.7 MB for a ~1.7 s run).
- **Never add an opam repo with `--set-default`.** It writes the repo into the
  opam *root's* default set at priority 1, not the switch's — so it reconfigures
  the user's whole opam installation and then shadows `opam.ocaml.org` for every
  switch they create later. `_ensure_switch` did this with dra27's relocatable
  fork, which silently substituted a 5.5 dev snapshot (`5.5.0+dev0-2025-04-28`,
  no `Ptyp_functor`) for released 5.5.0 and broke both a third party's unrelated
  merlin install (ocaml/merlin#2108) and this repo's own tools switch — ppxlib's
  `ast_505.ml` stopped type-checking, so a cold macro-benches setup failed at its
  test build. Now opt-in via `relocatable: true`, switch-scoped. To audit a
  machine: `opam repo list --all`, then
  `opam repo remove relocatable -a --set-default`.
- **The `opam-compiler` plugin lives in `$(opam var root)/plugins/bin/`,
  symlinked into the switch that installed it** — so rebuilding the tools switch
  leaves it dangling and the next run dies in `runtime.py` with
  `CalledProcessError` whose real cause (`unknown command 'compiler'`) is only on
  the plugin's stderr. Both launch scripts now re-install it when the resolved
  binary is missing.
- **`PerfAndOllyAttach` PID discovery.** The benchmark runs behind a
  `python3 -c` wrapper that blocks on a pipe so perf can attach pre-`exec`;
  olly then needs the *right* `.events` file. Wrapper scripts that run OCaml
  helpers in `$(...)` subshells (coq's `ocamlfind printconf stdlib`) leave decoy
  `.events` files, so discovery prefers the wrapper's own PID and otherwise
  filters on alive + `/proc/<pid>/exe` not in a build-tool blocklist, with a 10 s
  deadline. If olly silently doesn't attach, that deadline or the blocklist is
  where to look.
- **`minheap` output:** `o` = ran/passed (heap big enough → search **lower**),
  `x` = OOM (too small → higher), `t` = timeout (too small), `.` = crash/retry.
  It only runs for runtimes with a `get_heapsize_modifier` (**`OCamlMMTk`**, via
  `MMTK_HEAP_SIZE_MB`); plain `OCaml`/`NativeExecutable` raise
  `NotImplementedError` / are skipped. The live log interleaves the harmless
  `already exists; skipping build` WARNING, so each `o`/`x` prints at the *start
  of the next line* — read the RESULT yaml, not the live log.
- **MMTk runtime (`type: OCamlMMTk`)** is a drop-in. Default repo is
  `udesou/ocaml-mmtk`; the shipped `mmtk_*.yml` configs override `repo:` to
  `fplaunchpad/ocaml-mmtk`. Three mechanisms make the stock scripts work:
  1. `get_command_prefix()` prepends `setarch <arch> -R` to every build/run
     command (MMTk's fixed-address metadata mmap flakes under ASLR).
  2. `get_build_env_overrides()` sets a build-time `MMTK_HEAP_SIZE_MB` (16384,
     `setdefault` semantics) and `LIBRARY_PATH=<switch>/lib/ocaml` — MMTk emits
     a bare `-lmmtk_ocaml` into `ocamlc -config`'s c_libraries, so third-party
     dune-configurator probes (lwt pthread, ctypes machdep, owl cblas) fail to
     link and mis-detect features without it. Proper fix belongs upstream.
  3. `_ensure_switch` swaps opam's global `wrap-build-commands` /
     `wrap-install-commands` to `["setarch" "<arch>" "-R"]` for the compiler
     build only, then restores them — bubblewrap blocks cargo's network *and*
     resets the no-randomize personality, so the bit must be re-applied on the
     build command itself.
  `MMTK_PLAN`/`MMTK_THREADS` are run-time `EnvVar` modifiers (`plan-…`,
  `threads-…` in `macro_base.yml`); prefer those name-value forms over the older
  flag modifiers, which the `config_id` drops.
- **Relocatable compilers / satellite switches** exist
  (`OCamlMacroBenchmarkSuite`, dra27 relocatable overlay added to every
  `_ensure_switch`) but the *active* macro path is the `~/macro-benches`
  monorepo via plain `OCamlBenchmarkSuite` — no satellite switches.
- **olly has no `--version`** — the contract derives its version from the
  binary's owning opam switch or git checkout (`contract/native.py`).

## Known-broken / inconsistent files (fix or avoid)

Verified 2026-07-30 by loading every shipped config through
`Configuration.from_file` + `validate()` + `validate_tags()`. Re-run that sweep
after touching `config.py` or any base config; it is the cheapest way to catch a
config-layering regression.

| File | Problem |
|---|---|
| `examples/{minheap,ocaml,runbms}_example.yml` | Upstream examples that `include:` `$RUNNING_NG_PACKAGE_DATA/...`; only loadable through `python3 -m running`, which sets that variable. Not usable as `CONFIG_FILE` from the shell scripts. `runbms_example.yml` *also* fails `validate()` (it declares four runtimes and runs a subset) — upstream predates that check. |
| `src/running/command/genadvice.py` | Not in `__main__.MODULES`; unreachable. Either register it or delete it. |
| `install_deps_linux.sh` / `install_deps_macos.sh` | Clone `github.com/udesou/benches`; the canonical remote (and what `~/benches` actually points at) is `github.com/ocaml-bench/benches`. |
| `experiments/mmtk_minheap.yml`, `mmtk_minheap_result.yml` | Referenced by older docs, but they exist only on the unmerged `mmtk-minheap` branch. `experiments/mmtk_minheap_findings.md` (the write-up) is here. |

**`validate()` is stricter than the configs it inherited.** "Runtime declared but
not referenced by any `configs:` entry" is an *error*, which rules out the
commented-menu style (declare several compilers, run one) that both
`ocaml_gc_sweep_example.yml` and upstream's `runbms_example.yml` were written in.
`resolve_class` already drops unreferenced runtimes, so nothing breaks if one is
left declared — the check is hygiene, not correctness. If that ergonomics cost
starts to bite, downgrading this one case to a warning (keeping
referenced-but-undeclared and compared-but-no-data as errors) is the fix; it was
left alone here deliberately rather than folded into a docs change.

## Per-session workflow

1. Read `README.md` (overview, how to add a benchmark/experiment), this file
   (internals + gotchas), and `~/.claude` memory for current project state.
2. An experiment config `includes:` a base; declare only experiment-specific
   bits, and change base scalars through `overrides:`.
3. Validate cheaply before a long run: `-d` (dry run) expands the whole config
   grid; `-i 1` plus a narrowed `overrides.benchmarks` gives an end-to-end check.
4. Don't commit run logs. Commit only when asked. No `Co-Authored-By: Claude`.
