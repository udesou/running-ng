# Running a benchmark

## The layers

```
run_ocaml_bench_gc_sweep.sh     shell entry point: tools switch, olly, PATH, env
        │                        (build_ocaml_binaries_gc_sweep.sh = build only)
        ▼
python3 -m running runbms        this repo: config resolution, compiler builds,
        │                        benchmark builds, modifier application, run loop
        ▼
benches/ · macro-benches/        the benchmark programs and their build scripts
```

To run an experiment, execute:

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/<your>.yml \
LOG_DIR=/tmp/<your> \
  bash run_ocaml_bench_gc_sweep.sh
```

`build_ocaml_binaries_gc_sweep.sh` takes the same variables and stops after the
builds, which is the cheap way to check that every benchmark compiles under
every runtime before committing to a sweep.

## Prerequisites

Run `install_deps.sh`. The minimum is Python 3 with `pyyaml`,
opam 2.2+, and [`olly`](https://github.com/tarides/runtime_events_tools) recent
enough to emit `max_rss_kb` from `olly gc-stats --json` (commit `977e33b`,
[PR #85](https://github.com/tarides/runtime_events_tools/pull/85)); the launch
script refuses to start on an older checkout.

Hardware counters are optional and per-OS: Linux uses `perf` (`perf stat ls` to
check access, `sudo sysctl kernel.perf_event_paranoid=1` if denied), FreeBSD uses
`pmcstat` (`kldload hwpmc`, no root needed after that), macOS has no backend at
all. A run with no counter backend is a supported configuration, not a failure:
olly and rusage still produce data.

## Subcommands

The scripts wrap the common cases. Everything is also reachable as
`python3 -m running <cmd>` (with `PYTHONPATH=src`, or after `pip install -e .`
as `running <cmd>`), which is how the less common ones are used.

| Command | Purpose |
|---|---|
| `runbms LOG_DIR CONFIG` | build then run. Wrapped by `run_ocaml_bench_gc_sweep.sh`. |
| `buildbms CONFIG` | build only. Wrapped by `build_ocaml_binaries_gc_sweep.sh`. |
| `minheap CONFIG RESULT.yml [-a N]` | binary-search the smallest heap each benchmark completes in. MMTk only. Resumable. |
| `adapt RUN_DIR` | convert a legacy (pre-`schema_version`) run into data-contract artifacts. |
| `fillin`, `log_preprocessor` | inherited from upstream. |

## What a run produces

Each invocation of the launch script creates
`$LOG_DIR/<hostname>-<YYYY-MM-DD-Day-HHMMSS>/` containing:

```text
runbms.yml                 the fully merged config, after any RUNNING_TAG filter
runbms_args.yml            the CLI arguments used
<bm>.<hfac>.<size>.<config>.<suite>.log       per-cell log
olly_<same base>.json                          per-cell olly NDJSON sidecar
perf_<same base>.json                          per-cell counter NDJSON sidecar
memtrace_<same base>.<invocation>.trace        raw allocation trace, per invocation
memtrace_<same base>.<invocation>.json         folded-stack summary of that trace
contract/                  data-contract artifacts (only if the config sets
  manifest.json            schema_version)
  measurements/{olly,perf}.ndjson
```

With `compress_logs: true`, the default when a config does not say
otherwise, logs and sidecars are gzipped; both OCaml bases set it to `false`.

The `.log` holds a prologue (command line, running-ng version, `date`/`w`/
`vmstat`/`top`, all environment variables, OS and CPU info), then the benchmark's
stderr, then `*****`, then the combined `{"olly": ..., "perf": ...}` JSON for that
invocation.

The **sidecars are the primary machine-readable output**: one compact JSON object
per line, one line per invocation, carrying `olly gc-stats --json` and the counter
tool's output verbatim. The `perf` key is used
for every counter backend; `counter_backend` records which tool actually ran.

The `memtrace_*` files appear only under a `MemtraceAttach` modifier, see
[modifiers.md](modifiers.md).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `RUNNING_BENCH_DIR` | `../benches` beside this repo | root of the microbenchmark repo. Synonym of `RUNNING_MACRO_BENCH_DIR` in `run_ocaml_bench_gc_sweep.sh`; resolved eagerly (must exist) only by `build_ocaml_binaries_gc_sweep.sh`. |
| `RUNNING_MACRO_BENCH_DIR` | falls back to `RUNNING_BENCH_DIR` | root of the `macro-benches` monorepo. Required by any config including `base/ocaml/macro_base.yml`. |
| `CONFIG_FILE` | `src/running/config/examples/baseline_micro.yml` | the experiment to run |
| `LOG_DIR` | `<repo>/gc-sweep-logs` | where run directories are created |
| `PYTHON` | `python3` | interpreter to use, for a virtualenv that is not active |
| `RUNNING_TAG` | unset | comma-separated tags to filter benchmarks by |
| `RUNNING_REUSE_SWITCHES` | unset | `1` reuses a `running-ng-*` switch from an earlier run instead of rebuilding it |
| `RUNNING_NG_COUNTER_BACKEND` | auto-detected | force `linux-perf`, `freebsd-pmc` or `none` |
| `RUNNING_REQUIRE_PERFORMANCE_GOVERNOR` | unset | `1` makes a non-performance CPU governor fatal instead of a warning |
| `RUNNING_NG_STATE_DIR` | `~/.cache/running-ng/` | where switch provenance is recorded |
| `OLLY_DIR` | `../runtime_events_tools`, else `~/runtime_events_tools` | `runtime_events_tools` checkout (version-checked, built if needed) |
| `OLLY_BIN` | `$OLLY_DIR/_build/install/default/bin` | directory containing the `olly` binary |
| `TOOLS_SWITCH` | first opam switch with `dune`, else `running-ng-tools` | switch providing `dune`/`ocamlfind`/`olly` |
| `RUNNING_CONTRACT_ADAPTER` | `contract-adapter/bin/adapter` | adapter binary used by `running adapt` |
| `OPAMROOT` | `~/.opam` | standard opam variable. Two concurrent runs sharing one opam root are refused; point overlapping runs at separate roots. |

The installers take `BENCHES_DIR`, `MACRO_BENCHES_DIR` and `OLLY_DIR` too: set
them to checkouts you already have and nothing is cloned.
