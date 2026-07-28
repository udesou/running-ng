# `running-ng` (OCaml Benchmarking Fork)

This version of `running-ng` has been extended to run OCaml benchmarks with GC parameter sweeps. For information about the original project, see [the upstream `running-ng` repository](https://github.com/anupli/running-ng) and its [documentation](https://anupli.github.io/running-ng/).

It orchestrates two companion benchmark repos:

- [**benches**](https://github.com/ocaml-bench/benches) — sandmark-derived OCaml microbenchmarks (138 programs across simple, multicore, with-deps, with-packages).
- [**macro-benches**](https://github.com/ocaml-bench/macro-benches) — DaCapo-style monorepo of real-world OCaml applications (menhir, cpdf, alt-ergo, coq/rocq, …) with all dependencies vendored.

Both companion repos work standalone; running-ng adds per-runtime opam switch management, modifier composition (GC tuning, perf counters, runtime-events ring sizing), and parameter sweeps.

## Quick Start

```bash
# 1. Install all dependencies (Linux or macOS):
bash ~/running-ng/install_deps.sh

# 2. Run the benchmark sweep:
./run_ocaml_bench_gc_sweep.sh

# Or with a custom benchmark directory:
RUNNING_BENCH_DIR=/path/to/benches ./run_ocaml_bench_gc_sweep.sh
```

The script expects a sibling `benches/` directory by default. Override with `RUNNING_BENCH_DIR`. Logs go to `gc-sweep-logs/` (override with `LOG_DIR`). The config file defaults to `src/running/config/examples/ocaml_gc_sweep_example.yml` (override with `CONFIG_FILE`).

### Macrobenchmarks

Real-world OCaml applications (20 tools, 31 benchmark programs) built from a
single dune monorepo ([macro-benches](https://github.com/ocaml-bench/macro-benches))
that vendors all dependencies via opam-monorepo.  Current benchmarks:

- **Text processing:** menhir (3 grammars), sedlex
- **Text/media:** cpdf (4 PDF operations)
- **SMT / Proof:** alt-ergo (3), coq/rocq (corelib_stress)
- **Static analysis:** frama-c (EVA: 2), goblint
- **GC stress:** ahrefs-devkit (4)
- **Compilers / JS:** ocamlc-self-compile, jsoo
- **Databases/Compilers:** irmin, ocamlformat, liquidsoap-lang
- **Compression/Concurrency:** decompress, eio
- **Data formats:** yojson
- **Numerics:** zarith, owl
- **Media:** liq-video-frames
- **Bioinformatics:** pplacer

Two macrobenchmark configs are shipped:

| Config | Purpose | Runtimes | Invocations |
|---|---|---|---|
| `macrobenchmarks_monorepo.yml` | Cross-runtime comparison at default GC | 5.4.1, trunk, OxCaml | 1 |
| `fp_flambda_macrobenchmarks.yml` | FP × flambda 2×2 sweep | 4 variants of 5.4.1 | 3 |

To run the monorepo-based configs:

```bash
# Clone and set up the macro-benches monorepo (one-time; ~10 min):
git clone https://github.com/ocaml-bench/macro-benches.git ~/macro-benches
cd ~/macro-benches && make setup

# Then run from running-ng:
cd ~/running-ng
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/macrobenchmarks_monorepo.yml \
  bash run_ocaml_bench_gc_sweep.sh
```

The monorepo approach vendors all OCaml dependencies into a single
dune workspace, ensuring identical source code across all runtimes —
the only variable is the compiler.  See
[macro-benches/README.md](https://github.com/ocaml-bench/macro-benches#readme)
for setup details, patches applied to vendored sources, and a list
of system dependencies.

### Selecting benchmarks by runtime-feature tag (`RUNNING_TAG`)

Every macrobench is tagged in `src/running/config/base/ocaml/macro_base.yml` under the `tags:` block, mapping each benchmark to the OCaml runtime mechanism it exercises on its hot path. Tags are source-grounded — each `exercised_by:` claim carries `verified_at:` file:line citations from the vendored sources or compiler-libs. See [macro-benches/README.md §"Runtime-feature coverage matrix"](https://github.com/ocaml-bench/macro-benches#runtime-feature-coverage-matrix) for the human-readable matrix.

To run only the benchmarks that exercise a specific tag, set the `RUNNING_TAG` environment variable:

```bash
# Run only the benchmarks that exercise Weak.Make on the hot path:
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
RUNNING_TAG=weak_refs \
CONFIG_FILE=src/running/config/experiments/macrobenchmarks_monorepo.yml \
  bash run_ocaml_bench_gc_sweep.sh
# → kept 3 program(s) across 1 suite(s)  (alt_ergo_{fill,yyll,unsat_smt2})

# Union of two tags (any program with either tag):
RUNNING_TAG=weak_refs,effects ...
# → 8 programs: 3 alt-ergo + eio_fiber_stream + 4 lavyek cells
```

Semantics:

- **Union across tags.** Comma-separated names are unioned — a program is kept if it appears under `exercised_by:` of *any* named tag.
- **Intersection with `benchmarks:`.** The filter never re-enables a program that is excluded by the experiment's `benchmarks:` block (e.g. the currently-disabled `macro-merlin: []` stays disabled).
- **Coverage-gap tags fail loudly.** Tags with empty `exercised_by:` (currently `ephemerons` and `kcas` — see the gap notes in `macro_base.yml`) error out rather than silently running nothing.
- **Typos fail loudly too.** Unknown tag names produce a `ValueError` listing the available tags.

The full set of 16 tags shipped today, grouped by category:

| Category | Tags |
|---|---|
| Coverage gaps (no benchmark — error if selected) | `ephemerons`, `kcas` |
| Single-feature (precise diagnostic signal) | `weak_refs`, `effects`, `domains`, `atomics`, `marshal`, `signals`, `lwt`, `off_heap_accounting` |
| Allocation shape (cross-cutting allocator patterns) | `custom_block_finalisation`, `bigarrays`, `ffi_bulk` |
| Eio / multicore (narrower than `effects`) | `eio_fibers`, `io_uring`, `pthread_affinity` |

The `tags:` block in `macro_base.yml` is the single source of truth — adding or modifying tags there propagates to every experiment config that includes the base. Tag validation (`apply_tag_filter`'s schema + reference checks) runs on every config load, so a typo in `exercised_by:` (e.g. renamed program) errors out before any benchmarks run.

### MMTk (`ocaml-mmtk`)

`type: OCamlMMTk` runs the benchmarks on [ocaml-mmtk](https://github.com/fplaunchpad/ocaml-mmtk) — an OCaml 5.5 fork whose garbage collector is [MMTk](https://www.mmtk.io/) instead of the stock runtime. It is a **drop-in runtime**: the same `run_ocaml_bench_gc_sweep.sh` / `build_ocaml_binaries_gc_sweep.sh` scripts work unchanged (no wrapper).

**Declare it** in a config's `runtimes:` block, by commit (built via `opam-compiler` from [ocaml-mmtk](https://github.com/fplaunchpad/ocaml-mmtk), set with `repo:`) or by pre-built executable:

```yaml
runtimes:
  ocaml-mmtk:
    type: OCamlMMTk
    repo: "https://github.com/fplaunchpad/ocaml-mmtk.git"
    commit: "94f37a64b22de3c61b837bd1fde562ab4dcdb59f"   # pick a 5.5+mmtk commit
  # ocaml-mmtk-exe:           # alternative: a compiler you built yourself
  #   type: OCamlMMTk
  #   executable: /path/to/_install/bin/ocaml
```

**Extra requirement:** **Rust/cargo** at `~/.cargo` — MMTk's static lib is built by cargo *inside* the compiler `make`. Everything else is automatic:

- **ASLR off.** Every MMTk build/run command is wrapped in `setarch <arch> -R` (`Runtime.get_command_prefix`) — MMTk's fixed-address metadata mmap flakes under ASLR. MMTk-only; stock runtimes are untouched.
- **Build env.** `get_build_env_overrides` sets a build-time `MMTK_HEAP_SIZE_MB` (16384) and `LIBRARY_PATH=<switch>/lib/ocaml` so dune-configurator probes can link `-lmmtk_ocaml`. An explicit export wins.
- **opam sandbox.** The compiler build temporarily swaps opam's `wrap-build-commands` to a plain `setarch` wrapper (dropping bubblewrap) so cargo can reach the network during `make`, then restores it.

**Plan + heap** are selected at run time via env vars (`MMTK_PLAN` is a plain `EnvVar` modifier in configs):

| Env var | Meaning |
|---|---|
| `MMTK_PLAN` | `Immix` (default) \| `StickyImmix` \| `GenImmix` \| `MarkSweep` \| `NoGC`. **Native code requires an Immix-family plan** (Immix / StickyImmix / GenImmix). |
| `MMTK_HEAP_SIZE_MB` | **Fixed** heap size in MB (a hard bound on the MMTk heap, *not* a soft max like stock OCaml). |
| `MMTK_THREADS` | GC worker-thread count. |
| `MMTK_VERBOSE` | Print `[mmtk]` init line + GC stats (count / time / objects copied) at exit. |

```yaml
modifiers:
  mmtk_immix:  { type: EnvVar, var: MMTK_PLAN, val: Immix }
  mmtk_sticky: { type: EnvVar, var: MMTK_PLAN, val: StickyImmix }
```

**Shipped configs:**

| Config | Purpose |
|---|---|
| `experiments/mmtk_macro.yml` | MMTk (Immix + StickyImmix) vs stock 5.5 across the macro suite |
| `experiments/mmtk_minheap.yml` | Per-(benchmark, plan) smallest-heap search (results in `mmtk_minheap_result.yml`) |

**Run the macro comparison:**

```bash
cd ~/running-ng
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/mmtk_macro.yml \
  bash run_ocaml_bench_gc_sweep.sh
```

**Minimum heap (`minheap`).** Because `MMTK_HEAP_SIZE_MB` is a fixed budget, the `minheap` command binary-searches the smallest heap each benchmark completes in (stock OCaml grows on demand and is skipped):

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches PYTHONPATH=src \
  python3 -m running minheap \
    src/running/config/experiments/mmtk_minheap.yml \
    src/running/config/experiments/mmtk_minheap_result.yml -a 2
```

> **Caveat — `minheap` measures the peak *on-heap live* set only.** Off-heap memory (Bigarray / GMP / other custom blocks `malloc`'d by the runtime) does **not** count against `MMTK_HEAP_SIZE_MB`, and MMTk does not yet pace collection on off-heap pressure. So off-heap-heavy benches (`owl_gc`, `zarith_pi`) bottom out at the search floor while their real RSS grows with the heap budget; their minheap is *not* a meaningful footprint. Only the large-live-set ("footprint") benches give a usable boundary.

**Known MMTk-only crashes** (excluded from the configs above, so the search/comparison stays clean): `alt_ergo_{fill,yyll,unsat_smt2}` → SIGSEGV (moving GC vs C-held custom blocks) and `pplacer_testsuite` → SIGABRT (channel finaliser during GC). See [macro-benches](https://github.com/ocaml-bench/macro-benches#readme) for current MMTk status of each benchmark.

## Prerequisites

Run `install_deps.sh` to install everything automatically, or set up manually:

- **Python 3** with `pyyaml`
- **opam** >= 2.2 with a switch providing `dune`, `ocamlfind`, and benchmark-specific packages (`domainslib`, `zarith`, `lwt`, `decompress`, `yojson`, etc.)
- **olly** (`runtime_events_tools`) — for GC statistics via runtime events. Set `OLLY_BIN` or build from source:
  ```bash
  git clone https://github.com/tarides/runtime_events_tools.git ~/runtime_events_tools
  cd ~/runtime_events_tools && dune build -p runtime_events_tools @install
  ```
  **Minimum version:** must include [PR #85](https://github.com/tarides/runtime_events_tools/pull/85) (commit `977e33b`) so `olly gc-stats --json` emits `max_rss_kb`. The launch script checks this and fails with a clear message if the local checkout is too old — run `git pull` inside `~/runtime_events_tools` and delete `_build/` to rebuild.
- **perf** (Linux only) — for hardware performance counters. `PerfAndOllyAttach` modifiers require `perf stat`. Check access with `perf stat ls`; you may need `sudo sysctl kernel.perf_event_paranoid=1`.
- **benches/** — the benchmark sources repository (cloned by `install_deps.sh`)

### `install_deps.sh`

The installer auto-detects the OS and delegates to `install_deps_linux.sh` or `install_deps_macos.sh`. It installs:

| Component | Linux | macOS |
|---|---|---|
| System packages | `apt` (`build-essential`, `autoconf`, `libgmp-dev`, `linux-tools-generic`, …) | `brew` (`autoconf`, `gmp`, `coreutils`, …) |
| opam | >= 2.2 (binary from GitHub if system version is too old) | >= 2.2 via Homebrew |
| opam switch | `5.4.0` with build tools + benchmark packages | same |
| olly | Built from source in `~/runtime_events_tools` | same |
| Python | `pyyaml` via pip | same |
| Benchmarks | Clones `benches/` if not present | same |

**Note:** `perf` is Linux-only. On macOS, use `olly_gc` or `time_stats` modifiers instead of `perf_grp1/2/3`.

## How It Works

The system has three layers:

1. **`run_ocaml_bench_gc_sweep.sh`** — thin shell entry point that sets environment variables and invokes the Python framework
2. **`running-ng`** (this project) — a Python benchmark orchestration framework
3. **`benches/`** — OCaml benchmark programs with build scripts

### The Config File

The YAML config (`src/running/config/examples/ocaml_gc_sweep_example.yml`) has 5 key sections:

#### `runtimes` — Which OCaml compilers to test

Each runtime is resolved by one of:
- **`version`** (e.g. `"5.4.0"`) — clones the OCaml repo, checks out the tag, builds, and caches in `/tmp/running-ng-ocaml-toolchains/`
- **`commit`** — same but checks out a specific commit hash
- **`executable`** — uses a pre-built ocaml binary directly

Use `repo` to point to a fork (default is `https://github.com/ocaml/ocaml.git`). Additional build parameters: `configure_args` (extra args for `./configure`) and `make_targets` (default `["world.opt"]`).

**OxCaml (Jane Street's fork):** Use `type: OxCaml` — it handles the different build system automatically (`autoconf`, `--enable-runtime5`, Dune-based `make install`) and builds a stock OCaml 5.4.0 bootstrap compiler if needed:
```yaml
oxcaml-release:
  type: OxCaml
  commit: "<oxcaml-commit-hash>"
  configure_args: ["--enable-poll-insertion", "--enable-multidomain"]
```

The default repo is `https://github.com/oxcaml/oxcaml.git`; override with `repo` if needed. Additional parameters: `configure_args` (extra args for `./configure`), `bootstrap_version` (stock OCaml version for bootstrapping, default `"5.4.0"`).

**OxCaml multicore:** To run multicore benchmarks on OxCaml, you **must** pass `--enable-poll-insertion` and `--enable-multidomain` in `configure_args`. Without these, domain creation fails at runtime with `"failed to allocate domain"`.

**OxCaml build isolation:** The OxCaml build creates a dedicated opam switch (`running-ng-oxcaml-build`) with the bootstrap compiler and exact pinned dependency versions from `oxcaml-dev.opam`, so it does not interfere with your user-level opam switches.

**Toolchain caching:** All runtimes are cached in `/tmp/running-ng-ocaml-toolchains/<version-or-commit>/`. Delete this directory to force a rebuild.

#### `suites` + `benchmarks` — What to run

Suites define available programs (path, args, timeout). The `benchmarks` section selects which programs are actually active. Three suite types:
- **`OCamlBenchmarkSuite`** — sequential benchmarks (simple single-file or dune-built)
- **`OCamlMulticoreBenchmarkSuite`** — same but enforces OCaml >= 5
- **`OCamlOxcamlBenchmarkSuite`** — like multicore, but **fails** if the runtime is not `type: OxCaml` (for benchmarks using OxCaml-specific APIs like `Domain.Safe`)

Suite paths use `${RUNNING_BENCH_DIR}` (for micro suites pointing at `~/benches/`) or `${RUNNING_MACRO_BENCH_DIR}` (for macro suites pointing at `~/macro-benches/`). Both are expanded at runtime from environment variables set by the launch shell.

Inside each benchmark, the build script consumes a separate, identical set of env vars across both repos (`RUNNING_OCAML_BENCH_DIR`, `RUNNING_OCAML_OUTPUT`, `RUNNING_OCAML_RUNTIME_NAME`, `RUNNING_OCAML_SWITCH`) — see [benches/README.md §Build Script Contract](https://github.com/ocaml-bench/benches#build-script-contract) and [macro-benches/README.md §Build scripts](https://github.com/ocaml-bench/macro-benches#build-scripts).

#### `modifiers` — How to tweak runs

- **`Wrapper`** (`time_stats`, `olly_gc`) — prepends commands like `/usr/bin/time` or `olly gc-stats` to the benchmark execution (legacy; `PerfAndOllyAttach` is preferred)
- **`OCamlRunParam`** (`s`, `o`, `a`, `re`, `md`) — templated values like `s={0}` that get expanded with sweep values and concatenated into the `OCAMLRUNPARAM` env var (e.g. `OCAMLRUNPARAM="s=32768,o=40"`)
- **`PerfAndOllyAttach`** (`perf_grp1`, `perf_grp2`, `perf_grp3`) — attaches both `perf stat --json` and `olly gc-stats --json` to the benchmark process simultaneously, producing structured JSON output with no custom parsing needed (see below)

##### Runtime events tuning (`re`, `md`)

When using `PerfAndOllyAttach` or `olly_gc`, olly reads GC events from the OCaml runtime events ring buffer. On GC-heavy multicore workloads, the default buffer size can overflow, causing "Lost N events" warnings.

- **`re`** (ring buffer size) — sets `OCAMLRUNPARAM e=<value>` (log2 words per domain). Default is 16 (64K words). Increase to reduce lost events.
- **`md`** (max domains) — sets `OCAMLRUNPARAM d=<value>`. Default is 128, but the total events file size is `max_domains * 2^e * 8 bytes`. With `e=21` and default `d=128`, the file is 2 GB, which causes `ftruncate` to fail. Set `d` to the actual number of domains used (main + spawned) to keep it reasonable.

Example: for a benchmark spawning 8 worker domains (9 total), use `re-23|md-9`.

**Note:** Even with large ring buffers, domain 0 (the main domain) may still lose events during `Gc.full_major` cycles that generate enormous event volumes. The olly statistics remain usable despite these losses.

#### `configs` — Base configurations

Each config string is `runtime | modifier1 | modifier2 | ...`. Example:
```yaml
configs:
  - "oxcaml-release|perf_grp1|re-23|md-9"
```

#### `config_sweep` — The GC parameter sweep

```yaml
config_sweep:
  s: [32768, 65536, 131072, 262144, 524288, 1048576, 2097152]
  o: [40, 60, 80, 100, 120, 150, 200]
```

The framework takes the **cross-product** of sweep values for any modifiers **not already present** in the base config. So a base config `"ocaml-release|perf_grp1"` with the above sweep generates 7x7 = 49 configs like:
```
ocaml-release|perf_grp1|s-32768|o-40
ocaml-release|perf_grp1|s-32768|o-60
...
ocaml-release|perf_grp1|s-2097152|o-200
```

Each `s-32768` expands the modifier template `s={0}` to `s=32768`, which becomes part of `OCAMLRUNPARAM`.

### Execution Flow (`runbms`)

1. **Parse config** — resolve runtimes, suites, modifiers
2. **Expand configs** via `config_sweep` cross-product
3. **Pre-build phase** — for each unique (benchmark, runtime) pair, `running-ng` activates the runtime's opam switch (created via `opam-compiler`) and runs the benchmark's `.build.sh` script. The build script receives env vars `RUNNING_OCAML_OUTPUT`, `RUNNING_OCAML_BENCH_DIR`, `RUNNING_OCAML_RUNTIME_NAME`, and `RUNNING_OCAML_SWITCH`; the compiler, dune, and installed packages are on `PATH` via the switch. Produces a named binary like `binarytrees-ocaml-release`.
4. **Run loop** — for each benchmark x invocation x config combination:
   - Parse config string to get runtime + modifiers
   - `benchmark.attach_modifiers(mods)` sets `OCAMLRUNPARAM` and attaches perf + olly
   - Execute: `./binarytrees-ocaml-release 21` with `OCAMLRUNPARAM="s=32768,o=40"`, perf and olly attached
   - Capture stderr into a log file, structured JSON into a sidecar
5. **Log files** (`.log`) — metadata: prologue (system info), benchmark stderr; the JSON companion after `*****`
6. **JSON sidecar files** (`.json`) — the primary output: NDJSON (one compact JSON object per invocation) combining `olly gc-stats --json` and `perf stat --json` output directly

### Hardware Performance Counters (`PerfAndOllyAttach`)

The `PerfAndOllyAttach` modifier collects both `perf stat --json` hardware counters and `olly gc-stats --json` GC telemetry in a single benchmark run, producing structured JSON with zero custom parsing. It works by:

1. Spawning the benchmark via a thin Python wrapper that blocks on a sync pipe
2. Attaching `perf stat --json -p <pid>` to the blocked process
3. Closing the pipe write end to release the child, which `execvp`s into the benchmark command
4. Scanning for the OCaml runtime events `.events` file to discover the actual benchmark PID
5. If the OCaml PID differs from the wrapper, re-attaching perf to the real benchmark PID
6. Attaching `olly gc-stats --json --attach` to the runtime events ring buffer
7. Collecting both JSON outputs and combining into `{"olly": {...}, "perf": [...]}`

Three pre-defined counter groups avoid PMU multiplexing (each fits in ~4 programmable + 3 fixed counters):

| Modifier | Counters | Purpose |
|---|---|---|
| `perf_grp1` | task-clock, page-faults, cycles, instructions | Baseline IPC |
| `perf_grp2` | task-clock, cycles, stalled-cycles-frontend, stalled-cycles-backend | Pipeline stalls |
| `perf_grp3` | task-clock, cycles, cache-misses, LLC-load-misses, dTLB-load-misses, iTLB-load-misses | Cache/TLB hierarchy |

Run one group at a time. Example config:
```yaml
configs:
  - "ocaml-release|perf_grp1"
```

**Requirements:**
- `perf` must be installed and accessible (Linux only; check with `perf stat ls`)
- `olly` must be on PATH (install via `opam install runtime_events_tools`, or set `OLLY_BIN` — the launch script prepends it to PATH)
- `perf_event_paranoid` may need to be lowered: `sudo sysctl kernel.perf_event_paranoid=1`

**macOS:** `perf` is not available. Use the `olly_gc` wrapper modifier instead (which runs `olly gc-stats` as a command prefix rather than attaching via PID), or rely on `time_stats` for basic timing.

### Benchmark Build Scripts

All build scripts assume the runtime's opam switch is activated (compiler +
dune on `PATH`). `running-ng` handles switch creation via `opam-compiler` and
environment activation automatically. Three patterns in `benches/`:

**Simple / multicore benchmarks** (e.g. `benches/simple/almabench/`):
```bash
dune build --root "${BENCH_DIR}" --profile release almabench.exe
cp "${BENCH_DIR}/_build/default/almabench.exe" "${OUT}"
```
All benchmarks use dune. Multicore benchmarks auto-install `domainslib` into
the active switch.

**With-packages benchmarks** (e.g. `benches/with_packages/benchmarksgame/`):
- Run `opam install <pkg> -y` to install dependencies into the active switch
- Then build with dune
- No manual package installation needed

**Macrobenchmarks** (e.g. `benches/macrobenchmarks/menhir/`):
- Install real-world tools (alt-ergo, coq, cpdf, etc.) via `opam install`
- Copy the installed binary as the benchmark executable

### Purpose of the GC Sweep

The goal is to measure how OCaml GC tuning parameters affect performance:
- **`s`** — minor heap size in words (default 262144)
- **`o`** — space overhead % triggering major GC (default 120)
- **`a`** — allocation policy

The sweep systematically explores this tradeoff space across all benchmarks, measuring the impact on GC frequency, runtime, and memory usage.

### Cleaning Build Artifacts

To clean compiled benchmark binaries and build directories:
```bash
cd ~/benches && make clean
```

This removes compiled objects (`.cmi`, `.cmx`, etc.), tagged binaries (`*-ocaml-*`, `*-oxcaml-*`), dune `_build` directories, and generated input data.

### Analysing Results

After a sweep, generate a CSV summary and plots:
```bash
python3 scripts/plot_gc_sweep.py gc-sweep-logs/<run-directory>
```

### Default Config

The example config (`ocaml_gc_sweep_example.yml`) ships with this default:
```yaml
configs:
  - "oxcaml-release|perf_grp1|re-23|md-9"
```

This collects baseline IPC perf counters (`perf stat --json`) and structured GC stats (`olly gc-stats --json` — allocations, collections, latencies, execution times) for every benchmark. No `/usr/bin/time` or `OCAMLRUNPARAM=v=...` needed — olly provides all timing and GC allocation data directly. The `config_sweep` then varies `s` (minor heap size) and `o` (space overhead) across the parameter grid.

### Log File Structure

Each benchmark run produces two files:

**`.log` file** — metadata + benchmark stderr:
```
-----
<command line with OCAMLRUNPARAM and full invocation>
running-ng v0.3.8
<timestamp>

<system info: uptime, vmstat, top, env vars, OS, CPU>
<benchmark stderr>
*****
<structured JSON (pretty-printed, for human inspection)>
```

**`.json` sidecar file** — the primary structured output (NDJSON, one compact object per invocation):
```json
{"olly":{"execution_times":{...},"allocations":{...},"collections":{...},"gc_latencies":{...}},"perf":[{"event":"cycles","counter-value":"123",...},{"event":"instructions","counter-value":"456",...}]}
```

The `olly` section is the direct output of `olly gc-stats --json`. The `perf` section is the direct output of `perf stat --json` (an array of counter objects). No custom parsers involved — both tools produce their native JSON.

The `.json` sidecar is the preferred input for analysis scripts. Old `.log` files without a sidecar are still supported via regex fallback in `plot_gc_sweep.py`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `RUNNING_BENCH_DIR` | `../benches` (relative to script) | Root of the benchmark sources |
| `LOG_DIR` | `gc-sweep-logs/` | Where log files are written |
| `CONFIG_FILE` | `src/running/config/examples/ocaml_gc_sweep_example.yml` | YAML config. Shipped configs: `examples/ocaml_gc_sweep_example.yml`, `experiments/macrobenchmarks_monorepo.yml`, `experiments/fp_flambda_macrobenchmarks.yml`, `experiments/gc_sweep_all_versions.yml`. Reusable bases live under `base/ocaml/`. |
| `RUNNING_MACRO_BENCH_DIR` | n/a | Root of the `macro-benches` monorepo (required for any config that includes `base/ocaml/macro_base.yml`: `experiments/macrobenchmarks_monorepo.yml`, `experiments/fp_flambda_*.yml`, `experiments/regression_*.yml`, `examples/smoke_macro.yml`) |
| `OLLY_BIN` | `~/runtime_events_tools/_build/install/default/bin` | Directory containing `olly` binary |
| `OPAMROOT` | `~/.opam` | Standard opam variable. Two concurrent runs sharing one opam root are refused (see below); point overlapping runs at separate roots. |
| `RUNNING_REUSE_SWITCHES` | unset | Set to `1` to reuse a `running-ng-*` opam switch left over from an earlier run. By default such a switch is **removed and rebuilt** so that the compiler and the pinned dune (`OCaml.DUNE_VERSION`) are exactly what this run provisioned, rather than whatever a previous run happened to install. Rebuilding recompiles the compiler from source (~10-20 min per runtime), so set this for long sweeps where you trust the existing switches. Switches provisioned earlier in the *same* run are always reused. |

### Concurrent runs and the opam root

Because a run rebuilds stale switches, two runs sharing an opam root would
corrupt each other — the second would delete a switch the first is building or
benchmarking against. Runs therefore take a lock on `$OPAMROOT/running-ng.lock`
and **refuse to start** (exit 1) if another run holds it:

```console
[ERROR] Another running-ng run is using the opam root /home/udesou/.opam
  holder: pid=12345 mode=exclusive cmd=... runbms ...
Refusing to start: this run would remove and rebuild opam switches that the
other run is using, which would corrupt both.
```

The lock is **exclusive** for a normal run and **shared** when
`RUNNING_REUSE_SWITCHES=1` (which mutates nothing), so any number of
reuse-mode runs may overlap while a rebuilding run still gets exclusivity.
Dry runs (`-d`) never take the lock. The lock is released by the kernel when
the process exits, so a crashed or killed run does not wedge it. To run two
benchmark campaigns at once, give each its own `OPAMROOT`.

## Development Setup

```console
virtualenv env
source env/bin/activate
pip install -U pip setuptools build[virtualenv]
```

To run an editable build: `pip install -e .`

## License

This project is licensed under the Apache License, Version 2.0.
