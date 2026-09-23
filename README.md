# running-ng (OCaml fork)

A benchmark orchestrator for the OCaml compiler. A benchmarking run can be described in a YAML file
(which compilers, which benchmarks, which GC parameters) and the orchestrator builds the compilers, the binaries 
for each benchmark against each compiler, running them and collecting metrics using, for instance, `perf`/`pmcstat` and `olly`.

This is a fork of [`running-ng`](https://github.com/anupli/running-ng) (a
JVM/DaCapo harness; [upstream docs](https://anupli.github.io/running-ng/))
extended with OCaml runtimes, OCaml benchmark suites, `OCAMLRUNPARAM` sweeps, and
runtime-events telemetry.

It drives two companion benchmark repos:

| Repo | What |
|---|---|
| [**benches**](https://github.com/ocaml-bench/benches) | sandmark-derived microbenchmarks (sequential, multicore, effects, numerical) |
| [**macro-benches**](https://github.com/ocaml-bench/macro-benches) | real-world OCaml applications in one vendored dune monorepo (menhir, coq, alt-ergo, frama-c, cpdf, jsoo, ...)|

## Quick start

### 1. Install dependencies

```bash
bash install_deps.sh
```

Auto-detects the OS and delegates to `install_deps_linux.sh` (apt),
`install_deps_macos.sh` (Homebrew) or `install_deps_freebsd.sh` (pkg). It
installs system packages, opam 2.2+, a tools switch, `pyyaml`, and clones as siblings: 
`benches`, `macro-benches` and `runtime_events_tools` (for `olly`, which the orchestrator
builds per runtime), skipping any of these that already exists.

Note that macro-benches vendors its dependencies once, so when running an experiment with the macro benchmarks also run:

```bash
make -C ../macro-benches setup
```

### 2. Run the benchmarks

Both suites are driven by the same script with the configuration 
specificing which set of benchmarks to run.

```bash
# every micro benchmark, one invocation each
RUNNING_BENCH_DIR=../benches \
CONFIG_FILE=src/running/config/examples/baseline_micro.yml \
LOG_DIR=/tmp/baseline_micro \
  bash run_ocaml_bench_gc_sweep.sh

# every macro benchmark, one invocation each
RUNNING_MACRO_BENCH_DIR=../macro-benches \
CONFIG_FILE=src/running/config/examples/baseline_macro.yml \
RUNNING_TAG=all_benches \
LOG_DIR=/tmp/baseline_macro \
  bash run_ocaml_bench_gc_sweep.sh
```

When running the macro-benchmarks you may use `RUNNING_TAG` to select a particular set
of benchmarks to run (unset runs the `default_run` step for each benchmarking tool). Tags also 
select benchmarks by the runtime feature they exercise, for instance `RUNNING_TAG=weak_refs` 
runs all macro-benchmarks that exercise weak references. For more information about tags, see
[docs/ocaml/tags.md](docs/ocaml/tags.md).

### 3. Run an experiment

An experiment consists of a configuration specifying: which compilers to compare, which
which GC parameters to sweep, over which benchmarks, and which metricts to collect. 
The configuration is defined by setting `CONFIG_FILE`. A few examples exist in `src/running/config/experiments/`, 
for example:

```bash
RUNNING_MACRO_BENCH_DIR=../macro-benches \
CONFIG_FILE=src/running/config/experiments/macro_fp_flambda_5.5.1.yml \
LOG_DIR=$PWD/gc-sweep-logs-fp-flambda-$(date +%F) \
  bash run_ocaml_bench_gc_sweep.sh
```

which builds OCaml 5.5.1 four ways (stock, frame pointers, flambda, both) and
runs the whole set of macro benchmarks under each runtime variant. 

To write your own experiment, see [docs/ocaml/adding-an-experiment.md](docs/ocaml/adding-an-experiment.md). 
To test whether everything gets build sucessfully, which might be useful for larger experiments, run the `build_ocaml_binaries_gc_sweep.sh` script first instead.

## What a run produces

Each run generates a set of artifacts inside `$LOG_DIR/<hostname>-<timestamp>/`. This folder holds the merged config, one log per cell,
and per-cell `olly_*.json` / `perf_*.json` sidecars (one JSON object per
invocation) when using `olly` or `perf/pmcstat`. Configs that set `schema_version` also write `contract/`, which
follows a [data contract](https://github.com/udesou/ocaml-bench-dashboard/blob/main/lib/schema/contract.ml)
and is ready to be ingested by the dashboard. The full layout in described in
[docs/ocaml/running.md](docs/ocaml/running.md).

## Analysing results

To visualize and analyze the reulsts, use the instructions inside [ocaml-bench-dashboard](https://github.com/udesou/ocaml-bench-dashboard), 
pointing the dashboard at a generated `contract/` directory for regression, absolute-value, sweep heatmaps and other views. For ad-hoc
analysis over the raw sidecars, `notebooks/` holds three Jupyter notebooks that can be constructed from the generated results; see
[notebooks/README.md](notebooks/README.md) for more information.

## Documentation

[docs/ocaml/](docs/ocaml/) covers the OCaml side in detail: [running a
benchmark](docs/ocaml/running.md), [what a config consists
of](docs/ocaml/configs.md), [runtimes](docs/ocaml/runtimes.md),
[modifiers](docs/ocaml/modifiers.md), [tags](docs/ocaml/tags.md), [adding a
benchmark](docs/ocaml/adding-a-benchmark.md) and [adding an
experiment](docs/ocaml/adding-an-experiment.md).

[CLAUDE.md](CLAUDE.md) has the contributor conventions, the internals that matter
when something breaks, and the gotchas worth knowing (useful info for LLMs).

## Development

```console
virtualenv env && source env/bin/activate
pip install -U pip setuptools 'build[virtualenv]'
pip install -e '.[tests]'        # or .[notebook], .[zulip]
pytest tests/
```

## License

Apache License, Version 2.0.
