# `running-ng` (OCaml Benchmarking Fork)

This version of `running-ng` has been extended to run OCaml benchmarks with GC parameter sweeps. For information about the original project, see [the upstream `running-ng` repository](https://github.com/anupli/running-ng) and its [documentation](https://anupli.github.io/running-ng/).

## Quick Start

```bash
# From the running-ng directory:
./run_ocaml_bench_gc_sweep.sh

# Or with a custom benchmark directory:
RUNNING_BENCH_DIR=/path/to/benches ./run_ocaml_bench_gc_sweep.sh
```

The script expects a sibling `benches/` directory by default. Override with `RUNNING_BENCH_DIR`. Logs go to `gc-sweep-logs/` (override with `LOG_DIR`). The config file defaults to `src/running/config/ocaml_gc_sweep_example.yml` (override with `CONFIG_FILE`).

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

Use `repo` to point to a fork (default is `https://github.com/ocaml/ocaml.git`). For example, an OxCaml runtime:
```yaml
oxcaml:
  type: OCaml
  repo: "https://github.com/ocaml-flambda/flambda-backend.git"
  commit: "<oxcaml-commit-hash>"
```

#### `suites` + `benchmarks` — What to run

Suites define available programs (path, args, timeout). The `benchmarks` section selects which programs are actually active. Three suite types:
- **`OCamlBenchmarkSuite`** — sequential benchmarks (simple single-file or dune-built)
- **`OCamlMulticoreBenchmarkSuite`** — same but enforces OCaml >= 5
- **`OCamlOxcamlBenchmarkSuite`** — like multicore, but warns that an OxCaml runtime is required (for benchmarks using OxCaml-specific APIs like `Domain.Safe`)

All paths in the config use `${RUNNING_BENCH_DIR}` which is expanded at runtime from the environment variable set by the launch script.

#### `modifiers` — How to tweak runs

- **`Wrapper`** (`time_stats`, `olly_gc`) — prepends commands like `/usr/bin/time` or `olly gc-stats` to the benchmark execution
- **`OCamlRunParam`** (`s`, `o`, `a`, `gc_verbose`) — templated values like `s={0}` that get expanded with values and concatenated into the `OCAMLRUNPARAM` env var (e.g. `OCAMLRUNPARAM="s=32768,o=40"`)
- **`PerfAndOllyAttach`** (`perf_grp1`, `perf_grp2`, `perf_grp3`) — attaches both `perf stat` and `olly gc-stats` to the benchmark process simultaneously. Uses SIGSTOP/SIGCONT to freeze the child after fork so both tools can attach before any code runs. Requires `perf` and `olly` on PATH

#### `configs` — Base configurations

Each config string is `runtime | modifier1 | modifier2 | ...`. Example:
```yaml
- "ocaml-release|time_stats"
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
3. **Pre-build phase** — for each unique (benchmark, runtime) pair, run `benchmark.prepare(runtime)` which calls the benchmark's `.build.sh` script once. The build script receives env vars `OCAML_EXECUTABLE`, `OCAML_HOME`, `RUNNING_OCAML_OUTPUT`, etc., and compiles the benchmark to a named binary like `binarytrees-ocaml-release`.
4. **Run loop** — for each benchmark x invocation x config combination:
   - Parse config string to get runtime + modifiers
   - `benchmark.attach_modifiers(mods)` sets `OCAMLRUNPARAM` and wraps with `/usr/bin/time`
   - Execute: `[/usr/bin/time ...] ./binarytrees-ocaml-release 21` with `OCAMLRUNPARAM="s=32768,o=40"`
   - Capture stdout+stderr into a log file
5. **Log files** are named like: `binarytrees.1000.0.ocaml-release.time_stats.s-32768.o-40.multicore-effects.log`
   - Prologue includes system info (vmstat, cpuinfo, env vars)
   - Body is the benchmark's raw output + `/usr/bin/time` stats

### Benchmark Build Scripts

Two patterns in `benches/`:

**Simple benchmarks** (e.g. `benches/simple/binarytrees/binarytrees.build.sh`):
```bash
"${OCAMLOPT}" -O3 -I +unix unix.cmxa source.ml -o "${RUNNING_OCAML_OUTPUT}"
```

**Multicore benchmarks** (e.g. `benches/multicore/multicore-numerical/`):
- More complex — use `ocamlfind` with `-package domainslib`
- Auto-create opam switches for custom compilers
- Require OCaml 5+

### Purpose of the GC Sweep

The goal is to measure how OCaml GC tuning parameters affect performance:
- **`s`** — minor heap size in words (default 262144)
- **`o`** — space overhead % triggering major GC (default 120)
- **`a`** — allocation policy

The sweep systematically explores this tradeoff space across all benchmarks, measuring the impact on GC frequency, runtime, and memory usage.

### Hardware Performance Counters (`PerfAndOllyAttach`)

The `PerfAndOllyAttach` modifier collects both `perf stat` hardware counters and `olly gc-stats` GC telemetry in a single benchmark run. It works by:

1. Forking the benchmark process in a SIGSTOP state
2. Attaching `perf stat -p <pid>` and `olly gc-stats -p <pid>` to the frozen process
3. Sending SIGCONT to let the benchmark run
4. Collecting both outputs when the benchmark exits

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
- `perf` must be installed and accessible (check with `perf stat ls`)
- `olly` must be on PATH (install via `opam install runtime_events_tools`, or set `OLLY_BIN` — the launch script prepends it to PATH)
- `perf_event_paranoid` may need to be lowered: `sudo sysctl kernel.perf_event_paranoid=1`

### Default Config

The example config (`ocaml_gc_sweep_example.yml`) ships with this default:
```yaml
configs:
  - "ocaml-release|time_stats|perf_grp1|gc_verbose"
```

This collects `/usr/bin/time` resource stats, baseline IPC perf counters, olly GC stats, and verbose GC output (`OCAMLRUNPARAM=v=0x400`) for every benchmark. The `config_sweep` then varies `s` (minor heap size) and `o` (space overhead) across the full parameter grid.

### Analysing Results

After a sweep, generate a CSV summary and plots:
```bash
python3 scripts/plot_gc_sweep.py gc-sweep-logs/<run-directory>
```

## Development Setup

```console
virtualenv env
source env/bin/activate
pip install -U pip setuptools build[virtualenv]
```

To run an editable build: `pip install -e .`

## License

This project is licensed under the Apache License, Version 2.0.
