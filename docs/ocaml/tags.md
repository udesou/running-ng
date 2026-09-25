# Selecting benchmarks by runtime-feature tag

`macro_base.yml` carries a `tags:` block mapping OCaml runtime features to
the benchmarks that exercise them (on their hot path). Every tag is defined by
an `exercised_by:` entry which carries `verified_at:` file:line citations into the
vendored sources, and, where the point was settled by measurement. 
The matrix of tags can be found in [macro-benches](https://github.com/ocaml-bench/macro-benches/#run-sweeps).

Set `RUNNING_TAG` to run only those benchmarks:

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
RUNNING_TAG=weak_refs \
CONFIG_FILE=src/running/config/experiments/macro_5.5.1.yml \
  bash run_ocaml_bench_gc_sweep.sh
# → kept 11 program(s) across 2 suite(s)   (every alt-ergo and frama-c program)

RUNNING_TAG=weak_refs,effects ...   # union of both tags
```

Note that: 

- **Union across tags**: use a comma-separated list.
- **Intersected with `benchmarks:`**: a tag never re-enables something the
  experiment disabled (`macro-merlin: []` stays off, e.g.).

The twelve runnable feature tags:

| Category | Tags |
|---|---|
| Memory management | `weak_refs`, `ephemerons`, `custom_blocks`, `bigarrays`, `off_heap_accounting`, `c_side_allocation` |
| Runtime primitives | `marshal`, `compare_hash` |
| Concurrency | `effects`, `domains`, `lwt` |
| Foreign code | `ffi_bulk` |

Six more tags select input-size ladders rather than features: `default_run` (one
rung per tool, 21 programs, auto-applied when `RUNNING_TAG` is unset), `small_run`
and `large_run` (21 each), `huge_run` (2: zarith and owl), `legacy` (30) and
`all_benches` (95).

Tag validation (making sure tags are sound) runs on every
`runbms`, whether or not `RUNNING_TAG` is set.
