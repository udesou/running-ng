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

<!-- TODO(docs): the example output is stale: weak_refs now keeps 11 programs across
     2 suites (all six alt-ergo programs plus all five frama-c ones, which hash-cons
     EVA's state maps through Weak.Make). -->

- **Union across tags**, comma-separated.
- **Intersected with `benchmarks:`**: a tag never re-enables something the
  experiment disabled (`macro-merlin: []` stays off).
- **Typos fail loudly**, with the list of available tags.

<!-- TODO(docs): the feature tags were re-derived from the benchmark sources on
     2026-09-21 and this section is stale. There are 14 now, not 16, and the membership
     of most changed: the input-size rungs are tagged too, where before only the
     pre-ladder anchors were. Gone: `kcas` (nothing in the tree calls it), `eio_fibers`
     (identical membership to `effects`), `atomics` and `mutex_condition` (same programs
     as `effects` + `domains`, and native atomics are inlined and uncontended, so no
     profile sees them), `pthread_affinity`, `foreign_threads`, `subprocess_spawn`
     (properties of how we measure, not of the runtime), `digests`, `lex_parse_engines`
     (runtime code nobody optimises), `signals` (one delivery per run measures nothing
     about the poll path), `memprof` (a MEMTRACE knob). `custom_block_finalisation` is
     now `custom_blocks`. `ephemerons` is no longer a gap: rocq's CEphemeron is an
     Ephemeron.K1-with-data table, reached by every coq rung because `Compute` is
     `vm_compute`. New: `compare_hash` (compare_val and caml_hash), `c_side_allocation`
     (C allocating in the OCaml heap), `ocaml_finalisers` (a gap: Gc.finalise proper,
     as opposed to a custom block's finalize op). The table should read:
       Coverage gaps (error if selected): `io_uring`, `ocaml_finalisers`
       Memory management: `weak_refs`, `ephemerons`, `custom_blocks`, `bigarrays`,
         `off_heap_accounting`, `c_side_allocation`
       Runtime primitives: `marshal`, `compare_hash`
       Concurrency: `effects`, `domains`, `lwt`
       Foreign code: `ffi_bulk`
     A tag exists only if it names runtime code someone actually edits AND selects a
     set no other tag already gives you; worth stating here. Per-tag detail, with
     citations, is in macro_base.yml. -->
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
