# Selecting benchmarks by runtime-feature tag

`macro_base.yml` carries a `tags:` block mapping each OCaml runtime mechanism to
the benchmarks that exercise it **on their hot path**. Claims are source-grounded:
every `exercised_by:` entry carries `verified_at:` file:line citations into the
vendored sources. The human-readable matrix lives in
[macro-benches](https://github.com/ocaml-bench/macro-benches).

Set `RUNNING_TAG` to run only those benchmarks:

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
RUNNING_TAG=weak_refs \
CONFIG_FILE=src/running/config/experiments/macro_5.5.1.yml \
  bash run_ocaml_bench_gc_sweep.sh
# → kept 3 program(s) across 1 suite(s)   (alt_ergo_{fill,yyll,unsat_smt2})

RUNNING_TAG=weak_refs,effects ...   # union of both tags
```

- **Union across tags**, comma-separated.
- **Intersected with `benchmarks:`**: a tag never re-enables something the
  experiment disabled (`macro-merlin: []` stays off).
- **Typos fail loudly**, with the list of available tags.

The 16 runtime-feature tags:

| Category | Tags |
|---|---|
| Coverage gaps (error if selected) | `ephemerons`, `kcas` |
| Single feature | `weak_refs`, `effects`, `domains`, `atomics`, `marshal`, `signals`, `lwt`, `off_heap_accounting` |
| Allocation shape | `custom_block_finalisation`, `bigarrays`, `ffi_bulk` |
| Eio / multicore | `eio_fibers`, `io_uring`, `pthread_affinity` |

Six more tags select input-size ladders rather than features: `default_run` (one
rung per tool, auto-applied when `RUNNING_TAG` is unset), `small_run`,
`large_run`, `huge_run`, `legacy`, and `all_benches`.

Tag validation (making sure tags are sound) runs on **every**
`runbms`, whether or not `RUNNING_TAG` is set.
