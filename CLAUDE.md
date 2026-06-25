# CLAUDE.md — working notes for agents & contributors on `running-ng`

Auto-loaded context for Claude Code (and a quick orientation for humans). Keep it
short and current. Deep reference lives in `~/PROJECT_SUMMARY.md`.

## What this is

- A Python benchmark-orchestration framework (fork of `running-ng`) used here to
  evaluate **OCaml** compiler/GC performance: it builds compilers (via
  `opam-compiler`), builds benchmark binaries, runs them with wrappers
  (`/usr/bin/time`, `perf`, `olly`), and sweeps GC parameters.
- Benchmarks live in sibling repos: micro = `~/benches`, macro = `~/macro-benches`.
- Entry points: `run_ocaml_bench_gc_sweep.sh` (build **+ run**) and
  `build_ocaml_binaries_gc_sweep.sh` (build **only**) → both set up a tools switch
  (dune/ocamlfind/olly on PATH) then call `python3 -m running <cmd> …`.

## Hard rules (do not violate)

- No "Claude"/Anthropic/Co-Authored-By: Claude in further commit messages.
- **Remote is `origin = github.com/udesou/running-ng`** (a personal fork). Default
  working branch is **`adding-ocaml-support`** (all the OCaml support); `master`
  is the upstream JVM/DaCapo lineage. Commit/push only when asked.
- **Don't commit `gc-sweep-logs*/`, `*-logs/`, or other run output** — they're huge
  and timestamped.
- **Config layering is law:** an experiment config `includes:` a base
  (`base/ocaml/{micro_base,macro_base}.yml`) and only declares `runtimes`,
  `configs`, `modifiers`, `config_sweep`, `invocations`. See gotchas for the
  merge rules — getting them wrong is the #1 config bug.
- Keep documentation files (eg. README.md) consistent with every commit. 

## Where things live (read first)

- `src/running/command/` — subcommands: `runbms` (build+run), `buildbms`
  (build-only), `minheap` (binary-search smallest heap), `fillin`, `log_preprocessor`.
- `src/running/` — `runtime.py` (OCaml / OxCaml / **OCamlMMTk** runtimes, opam-compiler
  switch mgmt), `benchmark.py` (build + run, modifier application), `modifier.py`
  (Wrapper, EnvVar, OCamlRunParam, PerfAndOllyAttach…), `suite.py`
  (`OCamlBenchmarkSuite`, `OCamlMacroBenchmarkSuite`…), `config.py`, `util.py`.
- `src/running/config/` — `base/ocaml/{micro_base,macro_base}.yml` (suites + modifiers +
  benchmark lists), `examples/`, `experiments/` (one-off lab configs incl. the
  `mmtk_*.yml` MMTk comparison/minheap configs).
- `run_ocaml_bench_gc_sweep.sh`, `build_ocaml_binaries_gc_sweep.sh`,
  `scripts/plot_gc_sweep.py`, `notebooks/`.

## Build / run

- Env vars: `RUNNING_BENCH_DIR` (=`~/benches`), `RUNNING_MACRO_BENCH_DIR`
  (=`~/macro-benches`), `CONFIG_FILE`, `LOG_DIR`, `OLLY_DIR` (runtime_events_tools).
- Commands (all under `python3 -m running`, or via the launch scripts):
  - `runbms LOG_DIR CONFIG` — build (skips if the output binary exists) then run.
  - `buildbms CONFIG` — build only. Writes a `<bin>.build-failed` sentinel on
    failure and **skips on retry** — delete the sentinel to rebuild.
  - `minheap CONFIG RESULT.yml [-a N]` — per-(benchmark,config) binary search for the
    smallest heap. Needs binaries already built; writes RESULT incrementally
    (**resumable** — skips benches already recorded).
- Runtimes (in a config's `runtimes:`): `OCaml` (`version:`/`commit:`/`executable:`),
  `OxCaml`, `OCamlMMTk` (the MMTk fork). Each non-executable runtime gets an opam
  switch `running-ng-<name>` built by `opam compiler create`.

## Gotchas (hard-won — don't rediscover)

- **Config merge.** Including a base then redefining one of its **top-level scalars**
  (`invocations`, `remote_host`, `minheap_multiplier`, …) at top level → `combine()`
  `TypeError`. Change them through `overrides:` instead. The `benchmarks:` dict must
  also be set via `overrides:` (a top-level `benchmarks:` *merges/extends* the base's
  set rather than replacing it).
- **olly JSON sidecars are JSONL** — one line per invocation; don't infer invocation
  count from filenames.
- **Empty olly/perf output across a run** → check `/tmp` free space first (a tmpdir
  leak filled the tmpfs; the per-invocation runtime_events ring then SIGBUSes).
- **macro-lavyek** suites need `|re_par|md_par|pin_lavyek` in the config string, else
  olly drops events and `wall_time` goes negative.
- **`minheap` output:** `o` = ran/passed (heap big enough → search **lower**), `x` =
  OOM (too small → higher), `t` = timeout (too small), `.` = crash/retry. It only
  runs for runtimes with a `get_heapsize_modifier` (**`OCamlMMTk`**, via
  `MMTK_HEAP_SIZE_MB`); plain `OCaml`/`NativeExecutable` are skipped (no fixed heap).
  The live log interleaves the harmless `already exists; skipping build` WARNING, so
  each `o`/`x` prints at the *start of the next line* before the next size — read the
  RESULT yaml, not the live log.
- **MMTk runtime (`type: OCamlMMTk`)** is a drop-in: it builds udesou/ocaml-mmtk via
  opam-compiler (needs Rust/cargo at `~/.cargo`) and `Runtime.get_command_prefix`
  prepends `setarch <arch> -R` to every build/run command (MMTk's fixed-address
  metadata mmap flakes under ASLR), while `get_build_env_overrides` sets a build-time
  `MMTK_HEAP_SIZE_MB` + `LIBRARY_PATH=<switch>/lib/ocaml` (so dune-configurator probes
  can link `-lmmtk_ocaml`). So the *stock* scripts work with no wrapper. The opam
  build sandbox blocks cargo's network during `make`; `OCamlMMTk._ensure_switch`
  swaps `wrap-build-commands` to a plain `setarch` wrapper (drops bubblewrap) for the
  compiler build only, then restores it. `MMTK_PLAN` (Immix/StickyImmix for native)
  is a run-time `EnvVar` modifier.
- **Relocatable compilers / satellite switches** exist (`OCamlMacroBenchmarkSuite`,
  dra27 relocatable fork) but the *active* macro path is the `~/macro-benches`
  **monorepo** via plain `OCamlBenchmarkSuite` — no satellite switches.

## Per-session workflow

1. Read `~/PROJECT_SUMMARY.md` (config structure, execution flow, build contract) and
   the `~/.claude` memory for current project state.
2. An experiment config `includes:` a base; declare only runtime-specific bits.
3. Don't commit run logs; commit only when asked; `Co-Authored-By: Claude` is fine.
