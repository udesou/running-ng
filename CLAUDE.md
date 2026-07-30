# CLAUDE.md — working notes for agents & contributors on `running-ng`

Auto-loaded context for Claude Code (and orientation for contributors).
`README.md` is the human-facing overview: what the tool is, how to run it, how
to add a benchmark or an experiment. This file holds what doesn't belong there —
the internals you need when something breaks, the exact contracts, the
hard-won gotchas, and the current list of known-broken files.

## What this is

- A Python benchmark-orchestration framework (fork of `running-ng`, upstream
  `anupli/running-ng`) used here to evaluate **OCaml** compiler/GC performance:
  it builds compilers (via `opam-compiler`), builds benchmark binaries per
  runtime, runs them under `perf` + `olly`, sweeps GC parameters, and emits
  data-contract artifacts.
- Benchmarks live in sibling repos: micro = `~/benches` (13 suites, 200 enabled
  programs), macro = `~/macro-benches` (22 suites, 31 enabled programs; merlin
  and lavyek disabled). Both are driven through `OCamlBenchmarkSuite`.
  The `knob-a-rungs` branch adds the small/default/large input-size rungs on top
  of these, taking macro to 56 enabled programs; this branch does not carry them.
- Entry points: `run_ocaml_bench_gc_sweep.sh` (build **+ run**) and
  `build_ocaml_binaries_gc_sweep.sh` (build **only**) → both find/create a tools
  switch (dune/ocamlfind), build/verify olly, put both on `PATH`, then call
  `python3 -m running <cmd> …`.
- Consumers: `~/ocaml-bench-dashboard` (owns the data contract + ingestor +
  dashboard), `notebooks/`, `scripts/plot_gc_sweep.py`.

## Hard rules (do not violate)

- **No "Claude"/Anthropic/`Co-Authored-By: Claude` in commit messages.**
- **Remote is `origin = github.com/udesou/running-ng`** (a personal fork).
  Default working branch is **`adding-ocaml-support`** (all the OCaml support;
  it is also `origin/HEAD`); `master` is the upstream JVM/DaCapo lineage.
  Commit/push only when asked.
- **Don't commit `gc-sweep-logs*/`, `*-logs/`, or other run output** — huge and
  timestamped.
- **Config layering is law:** an experiment `includes:` a base
  (`base/ocaml/{micro_base,macro_base}.yml`) and declares only `runtimes`,
  `configs`, `modifiers`, `config_sweep`, `comparisons`, and `overrides`. See
  "Config merge" below — getting it wrong is the #1 config bug.
- Keep docs consistent with every commit: `README.md`, this file, and the
  header comment of any config you touch.

## Branch state (read before editing docs)

`adding-ocaml-support` is the trunk of this fork; two topic branches sit on top
of it and neither is merged (as of 2026-07-30):

- **`max-rss-excl-ring`** (this branch) — the data contract: `contract-adapter/`,
  `src/running/contract/{emit,native,vocab}.py`, `src/running/command/adapt.py`,
  `schema_version:` in `macro_base.yml`, and the MMTk plan/threads configs.
- **`knob-a-rungs`** — the Knob-A input-size ladders (the small/default/large
  rungs in `macro_base.yml` plus one olly-pass config per ladder). Independent
  of the contract work; the two touch `macro_base.yml` in different places.

`README.md` here documents the contract, so if `adding-ocaml-support` is
published before these land, either merge them first or drop the "Data contract
→ dashboard" paragraph and the `adapt` row from the commands table.

## Where things live (read first)

- `src/running/command/` — subcommands registered in `__main__.MODULES`:
  `fillin`, `runbms`, `buildbms`, `minheap`, `log_preprocessor`, `adapt`.
  (`genadvice.py` exists but is **not** registered — dead code, not reachable
  as a subcommand.)
- `src/running/`
  - `runtime.py` — `OCaml` / `OxCaml` / `OCamlMMTk` / `NativeExecutable` + the
    JVM/JS lineage; opam-compiler switch management, satellite switches.
  - `benchmark.py` — `OCamlBuiltBinaryBenchmark` (build contract, binary
    caching, `.build-failed` sentinel) and the `PerfAndOllyAttach` run path.
  - `suite.py` — `OCamlBenchmarkSuite`, `OCamlMulticoreBenchmarkSuite`,
    `OCamlOxcamlBenchmarkSuite`, `OCamlMacroBenchmarkSuite`.
  - `modifier.py` — `OCamlRunParam`, `EnvVar`, `Wrapper`, `ProgramArg`,
    `PerfAndOllyAttach`, `ModifierSet`, `Companion`.
  - `config.py` — includes/overrides merge, `validate()`, `validate_tags()`,
    `apply_tag_filter()`.
  - `contract/` — native contract emission (`native.py` orchestrates,
    `emit.py` normalizes, `vocab.py` is generated from the OCaml contract).
  - `analysis/json_sidecars.py` — sidecar discovery/parsing (new per-tool form
    plus the old combined form).
- `src/running/config/` — `base/ocaml/{micro_base,macro_base}.yml`,
  `examples/` (smoke tests), `experiments/` (one file per lab run, plus a few
  `*.md` findings write-ups).
- `contract-adapter/` — OCaml legacy→contract adapter + `gen_contract_py.py`
  (regenerates `src/running/contract/vocab.py` from the contract).
- `run_ocaml_bench_gc_sweep.sh`, `build_ocaml_binaries_gc_sweep.sh`,
  `install_deps{,_linux,_macos}.sh`, `scripts/plot_gc_sweep.py`, `notebooks/`.
- `docs/` — upstream's mdBook (`docs/src/`, JVM-oriented) plus this fork's
  methodology notes (`benchmark-calibration-triage.md`,
  `benchmark-noise-and-comparison-plan.md`, `benchmark-coverage-gaps-plan.md`).

## Build / run

- Env vars: see the README table. The two that bite:
  `RUNNING_BENCH_DIR` is resolved **eagerly** by both launch scripts
  (`$(cd .../benches && pwd)` under `set -e`), so a missing `benches/` aborts
  even a macro-only run; and `RUNNING_MACRO_BENCH_DIR` is read only by YAML
  `${…}` expansion, so an unset value silently yields paths starting with the
  literal variable name.
- Commands (`python3 -m running`, or the launch scripts):
  - `runbms LOG_DIR CONFIG` — build (skips if the output binary exists) then run.
    Extra args after the script name are forwarded (`-i`, `--resume`, `-d`, …).
  - `buildbms CONFIG` — build only. Prints a per-benchmark OK/FAILED table.
  - `minheap CONFIG RESULT.yml [-a N]` — per-(benchmark,config) binary search
    for the smallest heap. Needs binaries built; writes RESULT incrementally
    (**resumable** — skips benches already recorded).
- Runtimes: `OCaml` (`version:` | `commit:`/`hash:` | `executable:`), `OxCaml`,
  `OCamlMMTk`. Each non-`executable` runtime gets an opam switch
  `running-ng-<runtime-name>` built by `opam compiler create`; the switch is
  the cache, so delete the switch (not a temp dir) to force a compiler rebuild.
  Only **OxCaml** uses `/tmp/running-ng-ocaml-toolchains/` (for source
  checkouts).
- `configure_args:` is honoured (passed as `--configure-command "./configure …"`).
  `make_targets:` is **not implemented** — don't put it in a config.

### Benchmark build contract

`OCamlBuiltBinaryBenchmark._run_build` activates the runtime's switch env,
overlays `build_env:` then `runtime.get_build_env_overrides()`, and runs the
build script with `cwd = benchmark_dir`, prefixed by
`runtime.get_command_prefix()` (`setarch <arch> -R` for MMTk, empty otherwise):

| Variable | Value |
|---|---|
| `RUNNING_OCAML_BENCH_DIR` | the benchmark's `path:` |
| `RUNNING_OCAML_OUTPUT` | `binary:` (with `{benchmark}`/`{runtime}` expanded) else `<benchmark>-<runtime>` under `path:` |
| `RUNNING_OCAML_RUNTIME_NAME` | the runtime's config-file name |
| `RUNNING_OCAML_SWITCH` | switch name, when not in `executable:` mode |

Post-conditions and caching:
- The script **must** create `RUNNING_OCAML_OUTPUT`, or the build is an error.
- An existing output binary means **skip the build** (warning:
  `already exists; skipping build`) unless `always_build: true`.
- A failed build touches `<output>.build-failed` and **subsequent runs refuse
  to retry** until the sentinel is deleted.
- In-memory binary cache is keyed on `runtime.get_cache_key()`, which includes
  the runtime's *config name* — so `ocaml-5.4.1` and `ocaml-5.4.1-flambda`
  never share a cache entry despite the same `version:`.

### Output naming

`<bm>.<hfac|0>.<size|0>.<config with "|"→".">.<suite>.log`, sidecars
`olly_<same base>.json` / `perf_<same base>.json`, `.gz` when
`compress_logs` is true (its default when unset is **true**; both OCaml bases
set it to `false`). Also written into the run dir: `runbms.yml` (merged config,
post-`RUNNING_TAG`) and `runbms_args.yml`.

## Gotchas (hard-won — don't rediscover)

- **Config merge.** Including a base then redefining one of its **top-level
  scalars** (`invocations`, `schema_version`, `compress_logs`, `remote_host`,
  `minheap_multiplier`, `heap_range`, `spread_factor`) at top level →
  `combine()` `TypeError`. Change them through `overrides:`. A top-level
  `benchmarks:` dict *merges/extends* the base's set rather than replacing it,
  so narrowing the suite must also go through `overrides:`.
- **`validate()` is strict about runtimes.** Declared-but-unused and
  compared-but-not-run are **errors**, not warnings. Comment out spare runtime
  declarations rather than leaving them in.
- **olly JSON sidecars are JSONL** — one line per invocation; don't infer
  invocation count from filenames.
- **Sidecars are per tool now** (`olly_*.json`, `perf_*.json`). The old single
  combined `<base>.json` is still *read* by `analysis/json_sidecars.py` for
  backward compat, but nothing writes it. The combined object is still embedded
  in the `.log` after `*****`.
- **Empty olly/perf output across a run** → check `/tmp` free space first (a
  tmpdir leak filled the tmpfs; the per-invocation runtime_events ring then
  SIGBUSes). `_run_with_perf_and_olly` now warns below 1 GiB free and always
  removes its tmpdir.
- **macro-lavyek** suites need `|re_par-22|md_par-8|pin_lavyek` in the config
  string, else olly drops events and `wall_time` goes negative (≈ −4.7M s = the
  system uptime, because per-domain wall time falls back to `now - boot_time`).
  The `_par` modifiers **require explicit values** — the bare token `re_par`
  renders the literal `e={0}` into `OCAMLRUNPARAM`.
- **`PerfAndOllyAttach` PID discovery.** The benchmark runs behind a
  `python3 -c` wrapper that blocks on a pipe so perf can attach pre-`exec`;
  olly then needs the *right* `.events` file. Wrapper scripts that run OCaml
  helpers in `$(...)` subshells (coq's `ocamlfind printconf stdlib`) leave decoy
  `.events` files, so discovery prefers the wrapper's own PID and otherwise
  filters on alive + `/proc/<pid>/exe` not in a build-tool blocklist, with a 10 s
  deadline. If olly silently doesn't attach, that deadline or the blocklist is
  where to look.
- **`minheap` output:** `o` = ran/passed (heap big enough → search **lower**),
  `x` = OOM (too small → higher), `t` = timeout (too small), `.` = crash/retry.
  It only runs for runtimes with a `get_heapsize_modifier` (**`OCamlMMTk`**, via
  `MMTK_HEAP_SIZE_MB`); plain `OCaml`/`NativeExecutable` raise
  `NotImplementedError` / are skipped. The live log interleaves the harmless
  `already exists; skipping build` WARNING, so each `o`/`x` prints at the *start
  of the next line* — read the RESULT yaml, not the live log.
- **MMTk runtime (`type: OCamlMMTk`)** is a drop-in. Default repo is
  `udesou/ocaml-mmtk`; the shipped `mmtk_*.yml` configs override `repo:` to
  `fplaunchpad/ocaml-mmtk`. Three mechanisms make the stock scripts work:
  1. `get_command_prefix()` prepends `setarch <arch> -R` to every build/run
     command (MMTk's fixed-address metadata mmap flakes under ASLR).
  2. `get_build_env_overrides()` sets a build-time `MMTK_HEAP_SIZE_MB` (16384,
     `setdefault` semantics) and `LIBRARY_PATH=<switch>/lib/ocaml` — MMTk emits
     a bare `-lmmtk_ocaml` into `ocamlc -config`'s c_libraries, so third-party
     dune-configurator probes (lwt pthread, ctypes machdep, owl cblas) fail to
     link and mis-detect features without it. Proper fix belongs upstream.
  3. `_ensure_switch` swaps opam's global `wrap-build-commands` /
     `wrap-install-commands` to `["setarch" "<arch>" "-R"]` for the compiler
     build only, then restores them — bubblewrap blocks cargo's network *and*
     resets the no-randomize personality, so the bit must be re-applied on the
     build command itself.
  `MMTK_PLAN`/`MMTK_THREADS` are run-time `EnvVar` modifiers (`plan-…`,
  `threads-…` in `macro_base.yml`); prefer those name-value forms over the older
  flag modifiers, which the `config_id` drops.
- **Relocatable compilers / satellite switches** exist
  (`OCamlMacroBenchmarkSuite`, dra27 relocatable overlay added to every
  `_ensure_switch`) but the *active* macro path is the `~/macro-benches`
  monorepo via plain `OCamlBenchmarkSuite` — no satellite switches.
- **olly has no `--version`** — the contract derives its version from the
  binary's owning opam switch or git checkout (`contract/native.py`).

## Known-broken / inconsistent files (fix or avoid)

Verified 2026-07-30 by loading every shipped config through
`Configuration.from_file` + `validate()` + `validate_tags()`. Re-run that sweep
after touching `config.py` or any base config; it is the cheapest way to catch a
config-layering regression.

| File | Problem |
|---|---|
| `examples/{minheap,ocaml,runbms}_example.yml` | Upstream examples that `include:` `$RUNNING_NG_PACKAGE_DATA/...`; only loadable through `python3 -m running`, which sets that variable. Not usable as `CONFIG_FILE` from the shell scripts. `runbms_example.yml` *also* fails `validate()` (it declares four runtimes and runs a subset) — upstream predates that check. |
| `src/running/command/genadvice.py` | Not in `__main__.MODULES`; unreachable. Either register it or delete it. |
| `install_deps_linux.sh` / `install_deps_macos.sh` | Clone `github.com/udesou/benches`; the canonical remote (and what `~/benches` actually points at) is `github.com/ocaml-bench/benches`. |
| `experiments/mmtk_minheap.yml`, `mmtk_minheap_result.yml` | Referenced by older docs, but they exist only on the unmerged `mmtk-minheap` branch. `experiments/mmtk_minheap_findings.md` (the write-up) is here. |

**`validate()` is stricter than the configs it inherited.** "Runtime declared but
not referenced by any `configs:` entry" is an *error*, which rules out the
commented-menu style (declare several compilers, run one) that both
`ocaml_gc_sweep_example.yml` and upstream's `runbms_example.yml` were written in.
`resolve_class` already drops unreferenced runtimes, so nothing breaks if one is
left declared — the check is hygiene, not correctness. If that ergonomics cost
starts to bite, downgrading this one case to a warning (keeping
referenced-but-undeclared and compared-but-no-data as errors) is the fix; it was
left alone here deliberately rather than folded into a docs change.

## Per-session workflow

1. Read `README.md` (overview, how to add a benchmark/experiment), this file
   (internals + gotchas), and `~/.claude` memory for current project state.
2. An experiment config `includes:` a base; declare only experiment-specific
   bits, and change base scalars through `overrides:`.
3. Validate cheaply before a long run: `-d` (dry run) expands the whole config
   grid; `-i 1` plus a narrowed `overrides.benchmarks` gives an end-to-end check.
4. Don't commit run logs. Commit only when asked. No `Co-Authored-By: Claude`.
