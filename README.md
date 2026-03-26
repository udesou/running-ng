# `running-ng` (OCaml Benchmarking Fork)

This version of `running-ng` has been extended to run OCaml benchmarks with GC parameter sweeps. For information about the original project, see [the upstream `running-ng` repository](https://github.com/anupli/running-ng) and its [documentation](https://anupli.github.io/running-ng/).

## Quick Start

```bash
# 1. Install all dependencies (Linux or macOS):
bash ~/running-ng/install_deps.sh

# 2. Run the benchmark sweep:
./run_ocaml_bench_gc_sweep.sh

# Or with a custom benchmark directory:
RUNNING_BENCH_DIR=/path/to/benches ./run_ocaml_bench_gc_sweep.sh
```

The script expects a sibling `benches/` directory by default. Override with `RUNNING_BENCH_DIR`. Logs go to `gc-sweep-logs/` (override with `LOG_DIR`). The config file defaults to `src/running/config/ocaml_gc_sweep_example.yml` (override with `CONFIG_FILE`).

### Macrobenchmarks

A separate config (`macrobenchmarks.yml`) runs real-world OCaml applications (alt-ergo, coq, cpdf, cubicle, frama-c, menhir) at default GC settings across compiler versions — no GC sweep, just baseline comparison:

```bash
CONFIG_FILE=src/running/config/macrobenchmarks.yml ./run_ocaml_bench_gc_sweep.sh
# Or build-only:
CONFIG_FILE=src/running/config/macrobenchmarks.yml ./build_ocaml_binaries_gc_sweep.sh
```

These benchmarks install tools via opam into isolated switches. First run is slow (opam installs); subsequent runs reuse cached switches. Benchmark sources and inputs live in `benches/macrobenchmarks/`.

## Prerequisites

Run `install_deps.sh` to install everything automatically, or set up manually:

- **Python 3** with `pyyaml`
- **opam** >= 2.2 with a switch providing `dune`, `ocamlfind`, and benchmark-specific packages (`domainslib`, `zarith`, `lwt`, `decompress`, `yojson`, etc.)
- **olly** (`runtime_events_tools`) — for GC statistics via runtime events. Set `OLLY_BIN` or build from source:
  ```bash
  git clone https://github.com/tarides/runtime_events_tools.git ~/runtime_events_tools
  cd ~/runtime_events_tools && dune build -p runtime_events_tools @install
  ```
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

The YAML config (`src/running/config/ocaml_gc_sweep_example.yml`) has 5 key sections:

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

All paths in the config use `${RUNNING_BENCH_DIR}` which is expanded at runtime from the environment variable set by the launch script.

#### `modifiers` — How to tweak runs

- **`Wrapper`** (`time_stats`, `olly_gc`) — prepends commands like `/usr/bin/time` or `olly gc-stats` to the benchmark execution
- **`OCamlRunParam`** (`s`, `o`, `a`, `gc_verbose`, `re`, `md`) — templated values like `s={0}` that get expanded with sweep values and concatenated into the `OCAMLRUNPARAM` env var (e.g. `OCAMLRUNPARAM="s=32768,o=40"`)
- **`PerfAndOllyAttach`** (`perf_grp1`, `perf_grp2`, `perf_grp3`) — attaches both `perf stat` and `olly gc-stats` to the benchmark process simultaneously (see below)

##### `gc_verbose` — OxCaml vs stock OCaml

OxCaml reshuffled the GC verbosity flags. The bit that prints GC statistics at exit (`CAML_GC_MSG_STATS` — minor/major collections, heap size, promoted words, etc.) is at different positions:

| Runtime | Modifier | OCAMLRUNPARAM | Hex value |
|---|---|---|---|
| Stock OCaml (4.x / 5.x) | `gc_verbose` | `v=0x400` | bit 10 |
| OxCaml | `gc_verbose_oxcaml` | `v=0x1000` | bit 12 |

OxCaml added new GC message categories (`DOMAIN`, `STW`, `MINOR_HEAP`, `MAJOR_HEAP`, `STACKS`) which shifted `STATS` from `0x400` to `0x1000`. Using the wrong value silently prints nothing useful. **Use `gc_verbose` for stock OCaml runtimes and `gc_verbose_oxcaml` for OxCaml runtimes.**

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
  - "oxcaml-release|time_stats|perf_grp1|gc_verbose_oxcaml|re-23|md-9"
```

#### `config_sweep` — The GC parameter sweep

```yaml
config_sweep:
  s: [32768, 65536, 131072, 262144, 524288, 1048576, 2097152]
  o: [40, 60, 80, 100, 120, 150, 200]
```

The framework takes the **cross-product** of sweep values for any modifiers **not already present** in the base config. So a base config `"ocaml-release|time_stats"` with the above sweep generates 7x7 = 49 configs like:
```
ocaml-release|time_stats|s-32768|o-40
ocaml-release|time_stats|s-32768|o-60
...
ocaml-release|time_stats|s-2097152|o-200
```

Each `s-32768` expands the modifier template `s={0}` to `s=32768`, which becomes part of `OCAMLRUNPARAM`.

### Execution Flow (`runbms`)

1. **Parse config** — resolve runtimes, suites, modifiers
2. **Expand configs** via `config_sweep` cross-product
3. **Pre-build phase** — for each unique (benchmark, runtime) pair, `running-ng` activates the runtime's opam switch (created via `opam-compiler`) and runs the benchmark's `.build.sh` script. The build script receives env vars `RUNNING_OCAML_OUTPUT`, `RUNNING_OCAML_BENCH_DIR`, `RUNNING_OCAML_RUNTIME_NAME`, and `RUNNING_OCAML_SWITCH`; the compiler, dune, and installed packages are on `PATH` via the switch. Produces a named binary like `binarytrees-ocaml-release`.
4. **Run loop** — for each benchmark x invocation x config combination:
   - Parse config string to get runtime + modifiers
   - `benchmark.attach_modifiers(mods)` sets `OCAMLRUNPARAM` and wraps with `/usr/bin/time`
   - Execute: `[/usr/bin/time ...] ./binarytrees-ocaml-release 21` with `OCAMLRUNPARAM="s=32768,o=40"`
   - Capture stdout+stderr into a log file
5. **Log files** are named like: `binarytrees.1000.0.ocaml-release.time_stats.s-32768.o-40.multicore-effects.log`
   - Prologue includes system info (vmstat, cpuinfo, env vars)
   - Body is the benchmark's raw output + `/usr/bin/time` stats
   - `*****` separator, followed by companion output (olly gc-stats + perf stat)

### Hardware Performance Counters (`PerfAndOllyAttach`)

The `PerfAndOllyAttach` modifier collects both `perf stat` hardware counters and `olly gc-stats` GC telemetry in a single benchmark run. It works by:

1. Spawning the benchmark via a thin Python wrapper that blocks on a sync pipe
2. Attaching `perf stat -p <pid>` to the blocked process
3. Closing the pipe write end to release the child, which `execvp`s into the benchmark command
4. Scanning for the OCaml runtime events `.events` file to discover the actual benchmark PID (which may differ from the wrapper PID if `/usr/bin/time` forks a child)
5. If the OCaml PID differs, re-attaching perf to the real benchmark PID
6. Attaching `olly gc-stats --attach` to the runtime events ring buffer
7. Collecting both outputs when the benchmark exits

Three pre-defined counter groups avoid PMU multiplexing (each fits in ~4 programmable + 3 fixed counters):

| Modifier | Counters | Purpose |
|---|---|---|
| `perf_grp1` | task-clock, page-faults, cycles, instructions | Baseline IPC |
| `perf_grp2` | task-clock, cycles, stalled-cycles-frontend, stalled-cycles-backend | Pipeline stalls |
| `perf_grp3` | task-clock, cycles, cache-misses, LLC-load-misses, dTLB-load-misses, iTLB-load-misses | Cache/TLB hierarchy |

Run one group at a time. Example config:
```yaml
configs:
  - "ocaml-release|time_stats|perf_grp1|gc_verbose"
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
  - "oxcaml-release|time_stats|perf_grp1|gc_verbose_oxcaml|re-23|md-9"
```

This collects `/usr/bin/time` resource stats, baseline IPC perf counters, olly GC stats (via `PerfAndOllyAttach`), and OxCaml verbose GC output (`OCAMLRUNPARAM=v=0x1000`) for every benchmark. The `config_sweep` then varies `s` (minor heap size) and `o` (space overhead) across the parameter grid.

For stock OCaml runtimes, use `gc_verbose` instead of `gc_verbose_oxcaml`:
```yaml
configs:
  - "ocaml-release|time_stats|perf_grp1|gc_verbose"
```

### Log File Structure

Each log file contains:

```
-----
<command line with OCAMLRUNPARAM and full invocation>
running-ng v0.3.8
<timestamp>

<system info: uptime, vmstat, top, env vars, OS, CPU>
<benchmark stdout + stderr (including /usr/bin/time stats)>
*****
<olly gc-stats output (lost events warnings, execution times, per-domain stats, GC latency profile)>

--- perf stat ---
<perf stat hardware counter output>
```

The section before `*****` is the benchmark's direct output. The section after is companion tool output (olly + perf).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `RUNNING_BENCH_DIR` | `../benches` (relative to script) | Root of the benchmark sources |
| `LOG_DIR` | `gc-sweep-logs/` | Where log files are written |
| `CONFIG_FILE` | `src/running/config/ocaml_gc_sweep_example.yml` | YAML config (also: `macrobenchmarks.yml`, `gc_sweep_all_versions.yml`) |
| `OLLY_BIN` | `~/runtime_events_tools/_build/install/default/bin` | Directory containing `olly` binary |

## Development Setup

```console
virtualenv env
source env/bin/activate
pip install -U pip setuptools build[virtualenv]
```

To run an editable build: `pip install -e .`

## License

This project is licensed under the Apache License, Version 2.0.
