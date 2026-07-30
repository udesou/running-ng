# running-ng (OCaml fork)

A benchmark orchestrator for OCaml compiler and GC work. You describe an
experiment in YAML, including which compilers, which benchmarks, which GC parameters,
and it builds the compilers, builds the benchmark binaries against each one,
runs them under `perf` and `olly`, and writes structured results.

This is a fork of [`running-ng`](https://github.com/anupli/running-ng)
(a JVM/DaCapo harness; [upstream docs](https://anupli.github.io/running-ng/))
extended with OCaml runtimes, OCaml benchmark suites, `OCAMLRUNPARAM` sweeps,
and runtime-events telemetry.

It drives two companion benchmark repos:

| Repo | What | Size |
|---|---|---|
| [**benches**](https://github.com/ocaml-bench/benches) | sandmark-derived microbenchmarks (sequential, multicore, effects, numerical) | 13 suites, 200 programs enabled |
| [**macro-benches**](https://github.com/ocaml-bench/macro-benches) | real-world OCaml applications in one vendored dune monorepo (menhir, coq, alt-ergo, frama-c, cpdf, jsoo, …) | 20 active suites, 31 programs enabled |

Both work standalone. running-ng adds per-runtime opam switch management,
modifier composition (GC knobs, perf counter groups, runtime-events ring
sizing), parameter sweeps, and machine-readable output.

- [Quick start](#quick-start)
- [What a run produces](#what-a-run-produces)
- [Writing a config](#writing-a-config)
- [Runtimes](#runtimes)
- [Modifiers](#modifiers)
- [Selecting benchmarks by runtime-feature tag](#selecting-benchmarks-by-runtime-feature-tag)
- [Commands](#commands)
- [Adding a benchmark](#adding-a-benchmark)
- [Adding an experiment](#adding-an-experiment)
- [Analysing results](#analysing-results)
- [Environment variables](#environment-variables)

## Quick start

### 1. Install dependencies

```bash
bash ~/running-ng/install_deps.sh
```

Auto-detects the OS and delegates to `install_deps_linux.sh` (apt) or
`install_deps_macos.sh` (Homebrew). It installs system packages, opam ≥ 2.2, a
`5.4.0` tools switch, `olly` (built from `~/runtime_events_tools`), `pyyaml`,
and clones `benches/` next to this repo if it isn't there.

You need, at minimum: Python 3 with `pyyaml`, opam ≥ 2.2, and
[`olly`](https://github.com/tarides/runtime_events_tools) recent enough to emit
`max_rss_kb` from `olly gc-stats --json` (commit `977e33b`,
[PR #85](https://github.com/tarides/runtime_events_tools/pull/85)) — the launch
script checks this and refuses to start on an older checkout. On Linux you also
want `perf` (`perf stat ls` to check access; `sudo sysctl
kernel.perf_event_paranoid=1` if denied). `perf` does not exist on macOS — use
the `olly_gc` or `time_stats` wrapper modifiers there instead of `perf_grp*`
(both are defined in `micro_base.yml`; a macro config would need to declare
them itself).

### 2. Run the micro smoke test

```bash
cd ~/running-ng
RUNNING_BENCH_DIR=~/benches \
CONFIG_FILE=src/running/config/examples/smoke_micro.yml \
LOG_DIR=/tmp/smoke_micro \
  bash run_ocaml_bench_gc_sweep.sh
```

Two cheap benchmarks, one runtime, one invocation. The first run builds an
OCaml 5.4.1 opam switch from source, so budget time for that; afterwards it is
a minute or two.

### 3. Run the macro smoke test

The macro suite needs the monorepo checked out and set up once (~10 min):

```bash
git clone https://github.com/ocaml-bench/macro-benches.git ~/macro-benches
cd ~/macro-benches && make setup
```

Then:

```bash
cd ~/running-ng
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/examples/smoke_macro.yml \
LOG_DIR=/tmp/smoke_macro \
  bash run_ocaml_bench_gc_sweep.sh
```

From there, swap `CONFIG_FILE` for a real experiment, e.g.
`src/running/config/experiments/macrobenchmarks_monorepo.yml` (cross-runtime at
default GC) or `experiments/fp_flambda_macrobenchmarks.yml` (frame-pointer ×
flambda 2×2).

> `run_ocaml_bench_gc_sweep.sh` resolves `RUNNING_BENCH_DIR` eagerly and exits
> if the directory is missing — set it (even for macro-only runs) or keep a
> `benches/` checkout beside the repo.

### Layers

```
run_ocaml_bench_gc_sweep.sh     shell entry point: tools switch, olly, PATH, env
        │                        (build_ocaml_binaries_gc_sweep.sh = build only)
        ▼
python3 -m running runbms        this repo: config resolution, compiler builds,
        │                        benchmark builds, modifier application, run loop
        ▼
benches/ · macro-benches/        the benchmark programs and their build scripts
```

## What a run produces

Each invocation of the launch script creates
`$LOG_DIR/<hostname>-<YYYY-MM-DD-Day-HHMMSS>/` containing:

```text
runbms.yml                 the fully merged config, after any RUNNING_TAG filter
runbms_args.yml            the CLI arguments used
<bm>.<hfac>.<size>.<config>.<suite>.log       per-cell log
olly_<same base>.json                          per-cell olly NDJSON sidecar
perf_<same base>.json                          per-cell perf NDJSON sidecar
contract/                  data-contract artifacts (only if the config sets
  manifest.json            schema_version — see Analysing results)
  measurements/{olly,perf}.ndjson
```

In the filename, `<config>` is the config string with `|` replaced by `.`, and
`<hfac>`/`<size>` are `0` for OCaml runs (they are heap-fraction fields
inherited from the JVM lineage). With `compress_logs: true` — the default when
a config doesn't say otherwise — logs and sidecars are gzipped to `.gz`; both
OCaml bases set it to `false`.

The `.log` holds a prologue (command line, running-ng version, `date`/`w`/
`vmstat`/`top`, all environment variables, OS and CPU info), then the
benchmark's stderr, then `*****`, then the combined `{"olly": …, "perf": …}`
JSON for that invocation.

The **sidecars are the primary machine-readable output**: one compact JSON
object per line, one line per invocation, carrying `olly gc-stats --json` and
`perf stat --json` output verbatim. No custom parsers are involved.

## Writing a config

Configs live under `src/running/config/`:

```text
base/ocaml/micro_base.yml   suites + benchmarks + modifiers for ~/benches
base/ocaml/macro_base.yml   ditto for ~/macro-benches, plus the runtime-feature `tags:` block
examples/                   smoke tests and a commented example
experiments/                one file per real experiment
```

**Layering is the model.** An experiment file `includes:` a base and declares
only what is specific to it — `runtimes`, `configs`, `modifiers`,
`config_sweep`, `comparisons`, `invocations`. The base owns the suites, the
benchmark lists, and the shared modifiers, so a change there propagates to
every experiment.

Merge rules (see [CLAUDE.md](CLAUDE.md) for the failure modes):

- Lists from base and experiment are **concatenated**; dicts are **merged**.
- A **top-level scalar** already set by the base (`invocations`,
  `schema_version`, `compress_logs`, `remote_host`, `minheap_multiplier`, …)
  must be changed through `overrides:`, not redeclared at top level.
- A top-level `benchmarks:` block **extends** the base's; to *replace* it, put
  it under `overrides:`.

### The five blocks

**`runtimes:`** — which compilers. See [Runtimes](#runtimes).

**`suites:` + `benchmarks:`** — `suites` declares the available programs (path,
args, timeout, build script); `benchmarks` selects which ones actually run.
Both normally come from the base. Suite types:

| Type | Behaviour |
|---|---|
| `OCamlBenchmarkSuite` | the workhorse — builds via the benchmark's build script, runs the binary |
| `OCamlMulticoreBenchmarkSuite` | same, but fails if the runtime is OCaml < 5 |
| `OCamlOxcamlBenchmarkSuite` | same, but fails unless the runtime is `type: OxCaml` (for `Domain.Safe`, prefetch intrinsics, …) |
| `OCamlMacroBenchmarkSuite` | per-benchmark *satellite* opam switches, so one build's `opam install` can't pollute another's. Not used by the current macro path, which is the vendored monorepo under a plain `OCamlBenchmarkSuite`. |

Paths use `${RUNNING_BENCH_DIR}` (micro) or `${RUNNING_MACRO_BENCH_DIR}`
(macro), expanded from the environment at load time.

**`modifiers:`** — how to tweak a run. See [Modifiers](#modifiers).

**`configs:`** — the cells to run, one string each:
`"<runtime>|<modifier>|<modifier>|…"`. A modifier takes a value with a dash:
`re-25` applies the `re` modifier with value `25`.

```yaml
configs:
  - "ocaml-5.4.1|perf_grp1|re-25|md-2"
  - "ocaml-d8bb46c|perf_grp1|re-25|md-2"
```

**`config_sweep:`** — cross-product expansion. For each base config string, any
sweep key **not already present** in that string is expanded over its values:

```yaml
config_sweep:
  s: [131072, 262144, 524288, 1048576, 2097152]
  o: [40, 80, 120, 150, 200]
```

turns each config above into 5 × 5 = 25 cells
(`ocaml-5.4.1|perf_grp1|re-25|md-2|s-131072|o-40`, …). Each `s-131072` expands
the modifier template `s={0}` into `s=131072`, which is concatenated into
`OCAMLRUNPARAM`.

### `comparisons:` and `schema_version:`

`comparisons:` declares which runtimes are meant to be compared, so downstream
tooling (the dashboard) knows what to plot:

```yaml
comparisons:
  - label: "version effect"
    a: ocaml-5.4.1
    b: ocaml-d8bb46c
    # mode: pairwise (default) | cartesian; a/b may be lists
```

Configs are validated before anything runs: unknown runtimes, runtimes declared
but never used, runtimes compared but never run, and malformed comparison
blocks are all hard errors.

`schema_version: "1.0"` (set in `macro_base.yml`, inherited by every macro
experiment) switches on **native data-contract emission** — `contract/` is
written as the run proceeds. Without it, the run is legacy-only and needs a
post-hoc `running adapt`.

## Runtimes

Each entry in `runtimes:` names a compiler. Non-`executable` runtimes are built
by `opam compiler create` into a switch named `running-ng-<runtime-name>`,
which is reused on later runs — delete the switch to force a rebuild.

```yaml
runtimes:
  ocaml-5.4.1:                 # by release tag
    type: OCaml
    version: "5.4.1"
  ocaml-d8bb46c:               # by commit
    type: OCaml
    commit: "d8bb46c39bf5fcafb513a8ba18e667d3f8c2600a"
  ocaml-5.4.1-fp-flambda:      # same source, different configure
    type: OCaml
    version: "5.4.1"
    configure_args: ["--enable-frame-pointers", "--enable-flambda"]
  ocaml-local:                 # a compiler you built yourself
    type: OCaml
    executable: "/path/to/bin/ocaml"
```

`repo:` selects a fork (default `https://github.com/ocaml/ocaml.git`); it must
be a GitHub URL, since `opam-compiler` resolves `user/repo:ref`. Use either
`version:` or `commit:`, not both.

### OxCaml

```yaml
oxcaml-trunk:
  type: OxCaml
  commit: "<sha>"
  configure_args: ["--enable-poll-insertion", "--enable-multidomain"]
```

Handles OxCaml's different build system (autoconf, `--enable-runtime5`,
Dune-based `make install`) and builds a stock bootstrap compiler if needed
(`bootstrap_version`, default `5.4.0`). Default repo is
`https://github.com/oxcaml/oxcaml.git`. Source checkouts are cached under
`/tmp/running-ng-ocaml-toolchains/`, and the build gets its own opam switch
(`running-ng-oxcaml-build`) so it can't disturb your switches.

**Multicore on OxCaml requires both `--enable-poll-insertion` and
`--enable-multidomain`** — without them domain creation fails at run time with
`failed to allocate domain`.

### MMTk

`type: OCamlMMTk` runs on [ocaml-mmtk](https://github.com/udesou/ocaml-mmtk) —
OCaml 5.5 with [MMTk](https://www.mmtk.io/) in place of the stock collector.
It is a drop-in: the same launch scripts work unchanged.

```yaml
runtimes:
  ocaml-mmtk:
    type: OCamlMMTk
    repo: "https://github.com/fplaunchpad/ocaml-mmtk.git"   # default: udesou/ocaml-mmtk
    commit: "cbc66e3efd8f9200f3e84f791a6b6dfc36efce8c"
```

The only extra prerequisite is **Rust/cargo at `~/.cargo`** — MMTk's static
library is built by cargo inside the compiler's `make`. Everything else is
automatic: ASLR is disabled for every MMTk build and run command, the build
gets a fixed MMTk heap and a `LIBRARY_PATH` that lets dune-configurator probes
link `-lmmtk_ocaml`, and opam's build sandbox is temporarily relaxed so cargo
can fetch crates. See [CLAUDE.md](CLAUDE.md) for why each of those is needed.

Plan and heap are run-time environment variables, supplied as modifiers:

| Env var | Meaning |
|---|---|
| `MMTK_PLAN` | `Immix` (default) · `StickyImmix` · `GenImmix` · `MarkSweep` · `NoGC`. **Native code needs an Immix-family plan.** |
| `MMTK_HEAP_SIZE_MB` | **Fixed** heap size (a hard bound, unlike stock OCaml's soft target) |
| `MMTK_THREADS` | GC worker threads |
| `MMTK_VERBOSE` | print `[mmtk]` init line and exit-time GC stats |

`macro_base.yml` ships `plan` and `threads` as name-value modifiers, so
`|plan-StickyImmix|threads-4` both sets the variables and records them as
distinct dimensions in the contract:

```bash
cd ~/running-ng
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/mmtk_macro.yml \
  bash run_ocaml_bench_gc_sweep.sh
```

Because `MMTK_HEAP_SIZE_MB` is a hard bound, `minheap` (see
[Commands](#commands)) can binary-search the smallest heap each benchmark
completes in — MMTk is the only runtime that supports it. Two caveats worth
knowing before reading those numbers: off-heap memory (Bigarray, GMP, other
custom blocks) does not count against the MMTk budget and is not GC-paced, so
off-heap-heavy benches (`owl_gc`, `zarith_pi`) bottom out at the search floor
while their real RSS grows with the budget; and `alt_ergo_{fill,yyll,unsat_smt2}`
(SIGSEGV, moving GC vs. C-held custom blocks) and `pplacer_testsuite` (SIGABRT,
channel finaliser during GC) crash under MMTk and are excluded from the shipped
MMTk configs.

## Modifiers

| Type | Effect |
|---|---|
| `OCamlRunParam` | appends a `key=value` to `OCAMLRUNPARAM`; `val: "s={0}"` templates the sweep value |
| `EnvVar` | sets an environment variable (`MMTK_PLAN`, `MMTK_THREADS`, …) |
| `Wrapper` | prepends a command (`/usr/bin/time`, `olly gc-stats`, `taskset`) |
| `ProgramArg` | appends arguments to the benchmark |
| `PerfAndOllyAttach` | attaches `perf stat --json` **and** `olly gc-stats --json` to the running process |
| `ModifierSet` | a named bundle of other modifiers |

Shipped in the OCaml bases:

| Name | Type | Meaning |
|---|---|---|
| `s`, `o`, `a` | `OCamlRunParam` | minor heap words (default 262144), space overhead % (default 120), allocation policy. `micro_base` only — experiments that sweep them on macro declare them locally, along with `M` (`custom_major_ratio`). |
| `re`, `md` | `OCamlRunParam` | runtime-events ring size `e=` (log2 words per domain) and max domains `d=` |
| `re_par`, `md_par`, `pin_lavyek` | ditto + `Wrapper` | the parallel-suite counterparts (macro only) |
| `plan`, `threads` | `EnvVar` | `MMTK_PLAN` / `MMTK_THREADS` |
| `perf_grp1/2/3` | `PerfAndOllyAttach` | the three counter groups below |
| `time_stats`, `olly_gc` | `Wrapper` | `/usr/bin/time …`, `olly gc-stats` as a command prefix. `micro_base` only. |
| `gc_verbose`, `gc_verbose_oxcaml` | `OCamlRunParam` | GC stats at exit. **OxCaml reshuffled the verbosity bits** — stock OCaml wants `v=0x400`, OxCaml wants `v=0x1000`; the wrong one silently prints nothing. `gc_verbose_oxcaml` is `micro_base` only. |

Modifiers can carry `excludes:` — a `suite → [programs]` map of benchmarks they
should *not* apply to. That is how the macro base routes one `(re, md)` pair to
the sequential benchmarks and a different one to the parallel suite.

### Sizing the runtime-events ring (`re`, `md`)

`olly` reads GC events out of the runtime-events ring buffer. If the ring is
too small, events are lost (`[ring_id=N] Lost … events`) and the derived
statistics degrade — in the worst case per-domain wall time falls back to
`now - boot_time` and you get a large negative `wall_time`.

- `re` = `OCAMLRUNPARAM e=` — log2 words **per domain**. OCaml's default is 16
  (64K words).
- `md` = `OCAMLRUNPARAM d=` — max domains. The events file is
  `md × 2^re × 8` bytes, so leaving `md` at its 128 default while raising `re`
  produces multi-GB files and `ftruncate` failures. Set `md` to the domains you
  actually use (main + spawned).

The macro base uses `re-25|md-2` for the sequential benchmarks (256 MB/domain
ring, 512 MB file) and `re_par-22|md_par-8` for the 8-domain parallel suite.
Even with a large ring, domain 0 can still lose events across a `Gc.full_major`;
the statistics stay usable.

### Hardware counters (`PerfAndOllyAttach`)

`perf_grp1/2/3` collect hardware counters and GC telemetry in the same run, as
structured JSON. Three groups, sized to fit the PMU without multiplexing — run
one group at a time:

| Modifier | Counters | For |
|---|---|---|
| `perf_grp1` | task-clock, page-faults, cycles, instructions | baseline IPC |
| `perf_grp2` | task-clock, cycles, stalled-cycles-frontend/backend | pipeline stalls |
| `perf_grp3` | task-clock, cycles, cache-misses, LLC-load-misses, dTLB/iTLB-load-misses | cache and TLB |

Mechanically, the benchmark is launched behind a tiny wrapper that blocks on a
pipe; `perf` attaches to the still-blocked PID; the pipe is released so the
wrapper `exec`s the benchmark; running-ng then finds the process's
runtime-events file and attaches `olly`. See [CLAUDE.md](CLAUDE.md) for the
details that matter when it misbehaves.

## Selecting benchmarks by runtime-feature tag

`macro_base.yml` carries a `tags:` block mapping each OCaml runtime mechanism
to the benchmarks that exercise it **on their hot path**. Claims are
source-grounded: every `exercised_by:` entry carries `verified_at:` file:line
citations into the vendored sources. The human-readable matrix lives in
[macro-benches](https://github.com/ocaml-bench/macro-benches#runtime-feature-coverage-matrix).

Set `RUNNING_TAG` to run only those benchmarks:

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
RUNNING_TAG=weak_refs \
CONFIG_FILE=src/running/config/experiments/macrobenchmarks_monorepo.yml \
  bash run_ocaml_bench_gc_sweep.sh
# → kept 3 program(s) across 1 suite(s)   (alt_ergo_{fill,yyll,unsat_smt2})

RUNNING_TAG=weak_refs,effects ...   # union of both tags
```

- **Union across tags**, comma-separated.
- **Intersected with `benchmarks:`** — a tag never re-enables something the
  experiment disabled (e.g. `macro-merlin: []` stays off).
- **Coverage-gap tags fail loudly.** Tags with an empty `exercised_by:` exist
  precisely to keep the gap discoverable, and error out rather than silently
  running nothing.
- **Typos fail loudly** with the list of available tags.

The 16 tags shipped today:

| Category | Tags |
|---|---|
| Coverage gaps (error if selected) | `ephemerons`, `kcas` |
| Single feature | `weak_refs`, `effects`, `domains`, `atomics`, `marshal`, `signals`, `lwt`, `off_heap_accounting` |
| Allocation shape | `custom_block_finalisation`, `bigarrays`, `ffi_bulk` |
| Eio / multicore | `eio_fibers`, `io_uring`, `pthread_affinity` |

Tag validation (every `exercised_by:`/`cold:` entry names a real suite and
program; every tag has either programs or a documented `gap:`) runs on **every**
`runbms`, whether or not `RUNNING_TAG` is set.

## Commands

Everything is `python3 -m running <cmd>` (with `PYTHONPATH=src`, or after
`pip install -e .` as `running <cmd>`). The two shell scripts wrap the common
cases and additionally set up the tools switch and `olly` on `PATH`.

| Command | Purpose |
|---|---|
| `runbms LOG_DIR CONFIG` | build then run. Wrapped by `run_ocaml_bench_gc_sweep.sh`. |
| `buildbms CONFIG` | build only — verify every benchmark compiles under every runtime before committing to a sweep. Wrapped by `build_ocaml_binaries_gc_sweep.sh`. |
| `minheap CONFIG RESULT.yml [-a N]` | binary-search the smallest heap each benchmark completes in. MMTk only. Resumable — re-running skips benchmarks already in `RESULT`. |
| `adapt RUN_DIR` | convert a legacy (pre-`schema_version`) run into data-contract artifacts. |
| `fillin`, `log_preprocessor` | inherited from upstream. |

Useful `runbms` flags: `-i N` (override invocations), `--resume <run-id>`
(skip cells whose log already exists), `--skip-oom N` / `--skip-timeout N`,
`-p PREFIX` (prefix the run id), `-d` (dry run), `-v` (debug logging).

## Adding a benchmark

Where the benchmark lives decides which repo you touch:

- a self-contained program, or one needing only opam packages → **`~/benches`**
- a real-world application with a dependency tree → **`~/macro-benches`**,
  which vendors every dependency so all runtimes compile identical source

Either way, the contract with running-ng is the same, and the last step —
registering the program — happens here.

### 1. Write the benchmark and its build script

running-ng activates the runtime's opam switch (compiler, `dune`, installed
packages on `PATH`) and then runs `<name>.build.sh` in the benchmark directory,
passing four environment variables. Honour them and the script works both under
running-ng and by hand:

| Variable | Meaning | Fallback when unset |
|---|---|---|
| `RUNNING_OCAML_BENCH_DIR` | directory holding this benchmark's sources | the script's own directory |
| `RUNNING_OCAML_OUTPUT` | path the built binary **must** be written to | `${BENCH_DIR}/<name>-${RUNTIME_NAME}` |
| `RUNNING_OCAML_RUNTIME_NAME` | runtime identifier, e.g. `ocaml-5.4.1` | `runtime` |
| `RUNNING_OCAML_SWITCH` | opam switch name, when there is one | unset |

```bash
#!/usr/bin/env bash
set -euo pipefail
BENCH_DIR="${RUNNING_OCAML_BENCH_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT="${RUNNING_OCAML_OUTPUT:-${BENCH_DIR}/<name>-${RUNNING_OCAML_RUNTIME_NAME:-runtime}}"

dune build --root "${BENCH_DIR}" --profile release <name>.exe
cp "${BENCH_DIR}/_build/default/<name>.exe" "${OUT}"
chmod +x "${OUT}"
```

The build must be **hermetic per runtime**: unset inherited opam/OCaml
variables that could leak another switch's `.cmi` files, and build into a
per-runtime directory (`--build-dir _build-${RUNNING_OCAML_RUNTIME_NAME}`) so
runtimes don't clobber each other. macro-benches does both; see
[benches §Build Script Contract](https://github.com/ocaml-bench/benches#build-script-contract)
and [macro-benches CLAUDE.md](https://github.com/ocaml-bench/macro-benches/blob/master/CLAUDE.md)
for the full templates.

Make it **long enough to measure**: aim for roughly 5–30 s. If a single
invocation is shorter, add an in-process iteration count read from `argv` or an
environment variable rather than looping the binary in a shell — `olly` attaches
to one process, so a shell loop silently measures only the first child.

### 2. Register it in a suite

Add the program to the right suite in `base/ocaml/micro_base.yml` or
`base/ocaml/macro_base.yml`, then enable it in the same file's `benchmarks:`
block:

```yaml
suites:
  macro-<tool>:
    type: OCamlBenchmarkSuite      # OCamlMulticoreBenchmarkSuite if it needs OCaml 5
    timeout: 600                    # seconds, per invocation
    programs:
      <name>:
        path: "${RUNNING_MACRO_BENCH_DIR}/benchmarks/<tool>"
        build_script: "<tool>.build.sh"    # optional: defaults to <name>.build.sh
        args: "…"                           # optional: run-time arguments
        # binary: "<tool>-{runtime}"        # optional: defaults to <name>-<runtime>
        # build_env: { FOO: bar }           # optional: extra build-time env
        # always_build: true                # optional: rebuild even if the binary exists

benchmarks:
  macro-<tool>:
    - <name>
```

`path` pointing at a directory (or the presence of `build_script`/`binary`/
`always_build`) puts running-ng in build mode. Defaults follow convention, so
`build_script` and `binary` are usually unnecessary.

### 3. Tag it (macro only)

If it exercises a runtime mechanism on its hot path, add it under that tag's
`exercised_by:` in the `tags:` block, with a `verified_at:` citation — this is
what `RUNNING_TAG` selects on, and `validate_tags()` will reject a stale
reference on the next run. Uses that exist but aren't hot go under `cold:`.

### 4. Verify

Point a small config at it (copy `examples/smoke_macro.yml` and narrow
`overrides.benchmarks` to your suite), then:

```bash
# builds only — every runtime in the config, no measurement
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/examples/<your-smoke>.yml \
  bash build_ocaml_binaries_gc_sweep.sh

# then one invocation end-to-end
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/examples/<your-smoke>.yml \
LOG_DIR=/tmp/newbench \
  bash run_ocaml_bench_gc_sweep.sh
```

Check the `olly_*.json` sidecar for a plausible `wall_time` and no lost events.
If events were lost, raise `re` (and keep `md` at the real domain count).

Finally, document it: benches and macro-benches each keep a per-benchmark
description — add yours there.

## Adding an experiment

An experiment is one YAML file under `src/running/config/experiments/`. Start
from the nearest existing one; `full_s_o_sweep_2026_05_16.yml` is a good
template for a sweep, `macrobenchmarks_monorepo.yml` for a plain cross-runtime
comparison.

```yaml
# =============================================================================
# <One line: what question does this run answer?>
# =============================================================================
# Grid, runtimes, expected cost, and the exact command to reproduce it.
# Total invocations = cells × runtimes × programs × invocations.
# =============================================================================

includes:
  - "../base/ocaml/macro_base.yml"

overrides:
  invocations: 5          # top-level scalars the base already sets go HERE

runtimes:
  ocaml-5.4.1:
    type: OCaml
    version: "5.4.1"
  ocaml-5.5.0:
    type: OCaml
    version: "5.5.0"

modifiers:                # only what the base doesn't already define
  s: { type: OCamlRunParam, val: "s={0}" }
  o: { type: OCamlRunParam, val: "o={0}" }

configs:
  - "ocaml-5.4.1|perf_grp1|re-25|md-2"
  - "ocaml-5.5.0|perf_grp1|re-25|md-2"

config_sweep:
  s: [131072, 262144, 524288]
  o: [80, 120, 200]

comparisons:
  - label: "5.4.1 → 5.5.0"
    a: ocaml-5.4.1
    b: ocaml-5.5.0
```

Rules that catch most mistakes:

- **Only override through `overrides:`.** Redeclaring a scalar the base already
  sets (`invocations`, `schema_version`, `compress_logs`, …) at top level is a
  `TypeError` at load time. A top-level `benchmarks:` block *extends* the base's
  rather than replacing it — narrow the suite through `overrides.benchmarks`.
- **Every runtime must appear in `configs:`, and — if you declare
  `comparisons:` — in a comparison too.** Dead declarations and uncovered
  runtimes are validation errors, not warnings.
- **Keep the ring modifiers on every config string.** If your `benchmarks:`
  block enables the parallel (lavyek) suite, the string must also carry
  `|re_par-22|md_par-8|pin_lavyek`; the `_par` modifiers require explicit
  values.
- **Reduce the grid before you commit to it.** `-d` dry-runs the whole
  expansion, and `-i 1` with a two-benchmark `overrides.benchmarks` gives a
  cheap end-to-end check.

Then run it, and record what came out — the experiment file's header comment is
the natural home for the command line and any caveats.

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/<your>.yml \
LOG_DIR=$PWD/gc-sweep-logs-<your>-$(date +%F) \
  bash run_ocaml_bench_gc_sweep.sh
```

Long sweeps are worth resuming rather than restarting: `--resume <run-id>`
skips cells whose log file already exists. Run output (`gc-sweep-logs*/`) is
gitignored — don't commit it.

## Analysing results

**Data contract → dashboard.** Configs with `schema_version` emit
`contract/manifest.json` + `contract/measurements/{olly,perf}.ndjson` during the
run. That is the input to [ocaml-bench-dashboard](https://github.com/udesou/ocaml-bench-dashboard),
which validates it and renders regression, GC, absolute-value and sweep views.
Older runs are converted with `running adapt <run-dir>`, which shells out to
`contract-adapter/` (build it once with `contract-adapter/build.sh`).

**Notebooks.** `notebooks/` holds three Jupyter analyses over the raw sidecars —
A: regression dashboard, B: runtime-behaviour explorer, C: GC parameter sweep —
sharing `macrobench_loader.py` and a parquet cache. `pip install -e '.[notebook]'`
for the dependencies; see [notebooks/README.md](notebooks/README.md).

**Quick plots.** `python3 scripts/plot_gc_sweep.py <run-dir>` writes a CSV
summary and sweep plots straight from a log directory.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `RUNNING_BENCH_DIR` | `../benches` beside this repo | root of the microbenchmark repo. Resolved eagerly by the launch scripts — must exist. |
| `RUNNING_MACRO_BENCH_DIR` | unset | root of the `macro-benches` monorepo. Required by any config including `base/ocaml/macro_base.yml`. |
| `CONFIG_FILE` | `src/running/config/examples/ocaml_gc_sweep_example.yml` | the experiment to run |
| `LOG_DIR` | `<repo>/gc-sweep-logs` | where run directories are created |
| `RUNNING_TAG` | unset | comma-separated runtime-feature tags to filter benchmarks by |
| `OLLY_DIR` | `../runtime_events_tools`, else `~/runtime_events_tools` | `runtime_events_tools` checkout (version-checked, built if needed) |
| `OLLY_BIN` | `$OLLY_DIR/_build/install/default/bin` | directory containing the `olly` binary |
| `TOOLS_SWITCH` | first opam switch with `dune`, else `running-ng-tools` | switch providing `dune`/`ocamlfind`/`olly` |
| `RUNNING_CONTRACT_ADAPTER` | `contract-adapter/bin/adapter` | adapter binary used by `running adapt` |

## Development

```console
virtualenv env && source env/bin/activate
pip install -U pip setuptools 'build[virtualenv]'
pip install -e '.[tests]'        # or .[notebook], .[zulip]
pytest tests/
```

Contributor conventions, the internals that matter when something breaks, and
the current list of known-broken configs are in [CLAUDE.md](CLAUDE.md).
`docs/` contains upstream's mdBook (JVM-oriented) plus this fork's benchmark
methodology notes.

## License

Apache License, Version 2.0.
