# Benchmark calibration triage

Working list of macro-benchmarks that fall outside the operating envelope,
based on the health view in `notebooks/B_runtime_behaviour.ipynb` §3
applied to the `obelisk-2026-04-21-Tue-103805` dataset (8 variants × 26
benchmarks × 3 invocations = 624 measurements). One entry per benchmark
needing attention; tick boxes as we work through them.

## Operating envelope (current target)

- Wall time: **0.5–60s**. Lower bound: startup overhead drowns the signal.
  Upper bound: iteration cost is bad enough to derail PR feedback.
- GC overhead: **1–50%** of wall time. Below 1% the workload doesn't
  exercise the GC; above 50% the workload is GC-bound and probably
  measuring the wrong thing (or genuinely a stress test, in which case
  tag it `slow` and exclude from default profile).
- Major collections: **≥ 5**. Anything less and we're not really
  testing major-GC behaviour.
- Relative IQR: **≤ 10%**. Higher means the measurement is noisy enough
  that we shouldn't draw conclusions without more invocations or better
  isolation.

The envelope is a configurable Python dict at the top of Notebook B
(`ENVELOPE = { ... }`); tweak it for different hardware classes.

## Triage queue

### Tier 1 — clear actions, do first

- [x] **`coqc_corelib_stress` — input shrunk; GC pathology is inherent**

  Original: wall 715s, GC 98%, RSS 4.4 GB. Pre-edit `coq_corelib_stress.v`:
  `fib 25` / `sum_to 2000` / `ack 3 10` / `make_tree 15`.

  Post-edit (`fib 23` / `sum_to 1000` / `ack 3 8` / `make_tree 13`):
  wall **52s**, GC **94%**, RSS 1.1 GB, total heap allocations 1.7 GB,
  promoted_pct 16.3%. Within wall envelope on slow hardware (~10–20s
  expected on faster CI machine), preserves memory pressure and GC
  character.

  Key finding: GC% essentially unchanged (98% → 94%) despite 14× wall
  reduction. The pathology is **constitutional**: Coq kernel reduction
  on unary nat allocates a constructor per `S`, so any sustained work
  in this benchmark style is allocation-heavy. The `GC_PATHOLOGICAL`
  envelope flag is correct but uninformative for this benchmark by
  design.

  Followup (defer): introduce a `tag: gc-stress` mechanism so the
  health view in Notebook B suppresses the `GC_PATHOLOGICAL` flag for
  benchmarks where high GC overhead is expected. Same shape as the
  `tag: compute-bound` proposal for `devkit_gzip` in Tier 4 below.

- [ ] **`alt_ergo_fill` — judgement call, needs decision**

  Data: wall **0.18s**, GC 43%, minor 124 / major 9, RSS 42 MB, IQR 11%.

  What it does: alt-ergo SMT solving on `fill.why` (1 goal, integer +
  bitwise reasoning — uses `to_uint8`, `to_uint16`, `lnot`, etc.).

  Investigation:

  - Alt-ergo takes only **one input file**; no multi-file mode.
  - The CLI options `--steps-bound` and `--timelimit` only **cap** work,
    they cannot increase it.
  - We already have two healthy alt-ergo benchmarks in the suite —
    `alt_ergo_yyll` (19s) and `alt_ergo_unsat_smt2` (15s) — both in
    envelope. `fill.why` is the third and the only one that's too short.
  - `fill.why` does exercise theory paths (integer + bitwise) that the
    other two may not, so dropping it has a coverage cost.

  Two options, judgement call:

  1. **Drop `alt_ergo_fill` from the suite.** Remove from `benchmarks:`
     in `macrobenchmarks_base.yml`. Cleanest, but loses bitwise-theory
     coverage.
  2. **Replace `fill.why` with a harder bitwise-reasoning `.why`
     problem.** Requires sourcing one — Why3 corpus, alt-ergo regression
     tests, etc. Preserves coverage but is real research effort.

  No safe automated fix here. Recommend discussing with someone who knows
  the alt-ergo coverage requirements before acting.

- [x] **`devkit_stre` (and `devkit_gzip`, `devkit_network`) — fixed via Sys.argv-based in-process loop**

  Same fix shape as pplacer + owl. Three .ml files
  (`stre_bench.ml`, `gzip_bench.ml`, `network_bench.ml`) all share the
  same `let () = bench_a (); bench_b (); ...` structure; wrapped each
  with `for _ = 1 to (Sys.argv.(1)) do ... done`. The shared
  `ahrefs-devkit.build.sh` wrapper drops the shell `for` loop and
  just `exec`s the binary with the arg passed through.

  Final args (with `re-25` ring):

  | Benchmark | Per-iter wall | Final arg | Final wall | gc% |
  |---|---|---|---|---|
  | `devkit_stre` | 0.47s | 30 | 13.8s | 5.5% |
  | `devkit_gzip` | 2.72s | 5 | 9.9s | 1.0% |
  | `devkit_network` | 4.29s | 4 | 17.0s | 4.5% |

  All three: 0 lost events, exit 0, single observable OCaml process,
  olly captures the full run.

  Note: `devkit_gzip` at gc%=1.0 is compute-bound, not GC-stress.
  That's not a calibration bug — gzip is a compute workload by
  nature. Candidate for `tag: compute-bound` once tags are wired up.

  Data (`devkit_stre`): wall **0.48s**, GC 5.5%, minor 253 / major 89,
  RSS 16 MB, IQR 6%.

  What it does: stress-tests `Stre` (string ops) with 8 hardcoded
  benchmarks — split storm, substring slicing, pattern ops,
  concatenation, etc. Each benchmark has fixed inner-loop counts
  (e.g. `for i = 1 to 1000 do`).

  Investigation: a single invocation of the binary completes in ~6 ms.
  The arg `80` is consumed by a **shell wrapper** in
  `ahrefs-devkit.build.sh` that runs the binary `N` times in a loop:
  ```bash
  ITERATIONS="${1:-1}"
  for _ in $(seq 1 "$ITERATIONS"); do
    "${REAL_EXE}" >/dev/null 2>&1
  done
  ```
  So `0.48s = 80 × (process startup + 6 ms of work)`. Bumping the arg
  to 2000 would give 12s of *process spawning + OCaml startup*, not 12s
  of OCaml work. The GC / IPC / allocation stats would reflect 80
  short-lived processes rather than one meaningful run.

  This pattern affects three benchmarks: `devkit_stre`, `devkit_gzip`,
  `devkit_network`. (`devkit_htmlstream` does *not* go through this
  wrapper — it runs the binary directly and is healthy at 25s. That's
  the model the other three should follow.)

  Recommended fix (invasive — your call):

  1. Modify `stre_bench.ml` (and `gzip_bench.ml`, `network_bench.ml`)
     to accept an iteration count from `Sys.argv.(1)` defaulting to 1,
     and wrap the main entry point with a `for _ = 1 to n do ... done`
     loop.
  2. Either drop the shell wrapper entirely from
     `ahrefs-devkit.build.sh`, or change it to pass the count through
     as one argument:
     ```bash
     "${REAL_EXE}" "${1:-1}"
     ```
     so a single OCaml process does the loop in-process.
  3. Bump the YAML arg from `80` to whatever makes the in-process loop
     run ~10s. With per-iteration work ≈ 6 ms, that's roughly `1700`.

  This keeps each benchmark's allocation pattern intact (we just do it
  more times in one process) and lets olly observe meaningful GC stats.

  Quick alternative if invasive change is undesired: drop these three
  from the suite and rely on `devkit_htmlstream` for devkit coverage.

  Files: `~/macro-benches/benchmarks/ahrefs-devkit/{stre,gzip,network}_bench.ml`,
  `ahrefs-devkit.build.sh`, `macrobenchmarks_base.yml`.

- [ ] **`menhir_ocamly` — judgement call, structural**

  Data: wall **0.40s**, GC 28%, minor 280 / major 13, RSS 48 MB, IQR 8%.

  What it does: parser-generation on `ocaml.mly` (3006 lines, the OCaml
  grammar) with `--list-errors -la 2 --no-stdlib --lalr`.

  Investigation: looked at the three menhir benchmarks side by side.
  Wall time scales roughly linearly with grammar size:

  | Bench | Grammar | Wall |
  |---|---|---|
  | `menhir_ocamly` | 3006 lines | 0.40s |
  | `menhir_sql_parser` | 5846 lines (1.9×) | 3.3s |
  | `menhir_sysver` | 12735 lines (4×) | 20s |

  This means **on a fast CI machine, all three would be sub-5s**. The
  whole menhir-benchmark family suffers from the same problem on
  faster hardware that `menhir_ocamly` shows here on slow hardware.
  `menhir_ocamly` is the canary, not the only victim.

  No clean fix:
  - Bigger grammar — but the point is testing parser generation on
    *the OCaml grammar specifically*. Can't substitute.
  - Iteration knob — menhir's Main is upstream code (`duniverse/menhir/`);
    modifying it to take an iteration multiplier is invasive and
    diverges from upstream.
  - Shell-wrapper loop — same structural problem as the devkit
    benchmarks. Would measure process startup, not menhir.

  Two viable paths, your call:

  1. **Drop `menhir_ocamly` from the suite.** `menhir_sql_parser` and
     `menhir_sysver` cover menhir adequately. Lose
     OCaml-grammar-specific coverage.
  2. **Accept and tag.** Mark it `tag: too-short-on-slow-hardware` so
     the health view's `TOO_SHORT` flag becomes informational rather
     than actionable. On a fast CI box it'll be even more too-short,
     but the regression-detection signal still works as long as N
     invocations is large enough.

  No safe automated fix here either. Recommend deferring until the
  CI-machine plan in
  [benchmark-noise-and-comparison-plan.md](./benchmark-noise-and-comparison-plan.md)
  is decided — the right answer depends on whether benchmarks are
  expected to be consistently in-envelope across hardware classes.

### Tier 2 — investigate first, then act

- [x] **`dune_bootstrap` — removed (subprocess-bound orchestrator)**

  Data: wall **55s**, GC **0.0%**, minor **11** / major **5**, RSS 14 MB,
  IQR 0.1%.

  What it did: bootstrapped `dune` from source via `ocaml boot/bootstrap.ml`.
  `bootstrap.ml` uses `Sys.command` to spawn subprocesses and ultimately
  `exec`s a `.duneboot.exe`, so the actual compiler work happened in child
  processes that olly couldn't observe. olly only saw the orchestrating
  parent (hence GC=0%, 11 minor / 5 major).

  Resolution: replaced in concept by `ocamlc_self_compile`, which exercises
  the same compiler internals (Ephemeron tables, Hashtbl, Marshal, AST
  allocation) but in a **single observable OCaml process**. Wall time is
  still a compiler-throughput metric, but olly's stats now reflect the
  workload. The orchestrator-level "end-to-end bootstrap time" signal
  was deemed not worth the noisy/uninformative parent-process measurement.

  Action: dropped 2026-05-06 from benchmarks/, configs, README, results
  tables. `ocamlc_self_compile` is the replacement.

- [x] **`pplacer_testsuite` — fixed via env-var in-process loop**

  Original data: wall 3.9s, GC 71%, minor 1973 / major 722.

  Investigation: the wrapper script defaulted to `ITERATIONS=8`,
  running `tests.exe` 8 times in a shell loop. Real wall time of one
  full invocation was **~32s**, but olly+perf attached to a single
  OCaml process (the first child) and reported its lifetime — **3.9s**.
  Other 7 children ran without observation.

  Fix applied (commit pending in macro-benches):

  1. **`vendor/pplacer/tests/tests.ml`** — patched to read
     `PPLACER_TEST_LOOP` env var and run the suite N times in one
     OCaml process. Correctness check only on the first iteration —
     at least one test (`guppy:gaussian:coastal.v.upwelling`) leaks
     state between runs. Patch recorded in
     `scripts/setup-monorepo.sh` (Patch 13) so it survives a
     re-vendor.
  2. **`benchmarks/pplacer/pplacer.build.sh`** — wrapper drops the
     shell `for` loop, sets `PPLACER_TEST_LOOP="${1:-1}"` and
     `exec`s `tests.exe`.
  3. **`src/running/config/macrobenchmarks_base.yml`** —
     `args: "8"` → `args: "3"` (each iteration ≈ 3.5s, so 3
     iterations ≈ 10–11s wall).

  Verified: wall scales linearly (`arg=1` → 3.96s, `arg=3` → 11.02s),
  exit code 0 in both cases, and **olly's sidecar reports the full
  11s** with 5499 minor / 2098 major collections (≈ 3× the
  per-iteration counts) — confirming the run is observed end-to-end.

  This is a **broader pattern**: any benchmark using a shell-loop
  wrapper may be silently reporting per-child stats. Three benchmarks
  still affected: `devkit_stre`, `devkit_gzip`, `devkit_network`,
  `owl_gc` (the Tier 1 entry above documents the same fix shape; the
  pplacer change is the reference implementation).

  Documentation: pattern documented in `macro-benches/README.md` under
  §"Iteration counts" with a porting checklist for the next benchmark.

### Tier 3 — high variance, need more data first

- [ ] **`liq_parse_typecheck` — direct exec; just needs more invocations**

  Data: wall **26s**, GC 22%, RSS 49 MB, **IQR 29%**.

  Build script: confirmed `cp` of `liq_bench.exe` directly — no shell
  wrapper. Single OCaml process per invocation, olly observes
  everything correctly. So the 29% IQR is real run-to-run variance.

  N=3 invocations gives unreliable IQR estimates. Increase
  `invocations:` to ≥10 in a follow-up run and re-measure. May also
  benefit from the system-noise fixes in
  [benchmark-noise-and-comparison-plan.md](./benchmark-noise-and-comparison-plan.md)
  (`drop_caches`, frequency lock, `taskset`) — a 26s benchmark has
  plenty of surface for cache and thermal drift.

- [x] **`owl_gc` — fixed via Sys.argv-based in-process loop**

  Original data: wall 1.9s, GC 31%, RSS 43 MB, IQR 22%. Per-iteration
  measurement under olly with the env-var loop reveals the actual
  per-pass cost is ~2.6s — the original arg=7 wrapper-loop was likely
  reporting per-child stats too, like pplacer.

  Fix applied (commit `a7712fd` in macro-benches):

  1. **`benchmarks/owl/owl_gc.ml`** — wraps the existing O(n²)
     Gromov-Wasserstein matrix-pair computation in a for-loop driven
     by `Sys.argv.(1)`. No env var needed since plain OCaml main
     doesn't conflict with any framework's argv parsing.
  2. **`benchmarks/owl/owl.build.sh`** — wrapper drops the shell
     `for` loop; just `exec`s the binary with the arg passed through.
  3. **`src/running/config/macrobenchmarks_base.yml`** — `args: "7"`
     → `args: "6"` (each iteration ≈ 2.6s, so 6 iterations ≈ 16s).

  Critical follow-up — **runtime_events ring-size interaction**: a
  single OCaml process running 5+ iterations of allocation-heavy work
  overflows the OCaml default ring (`re-23` = 8 MB) and olly silently
  reports lost events plus a corrupted `wall_time` (we saw 686535s).
  The fix is to bump `re-` in the runbms config string. We've changed
  every running-ng config from `re-23` to `re-25` (32 MB ring) — the
  one explicit comment in `macrobenchmarks_base.yml` is the only
  remaining `re-23` reference and is documentation, not config.

  Verified at arg=6 with `e=25`: wall=15.79s, gc=7.95s (50.3%),
  minor=62970 / major=62970, RSS=151 MB, lost_events=0, exit 0.
  Olly observes the full 16s end-to-end.

  Three benchmarks in the triple still affected: `devkit_stre`,
  `devkit_gzip`, `devkit_network` — same fix shape.

- [ ] **`zarith_pi` — direct exec; borderline IQR**

  Data: wall **7.9s**, GC 27%, RSS 72 MB, **IQR 12%**.

  Build script: confirmed `cp` of `zarith_pi.exe` directly — no shell
  wrapper. Arg `15000` is the precision (digits of π). Single OCaml
  process, olly observes correctly.

  Just over the 10% IQR threshold. Likely needs more invocations and
  the system-noise fixes; don't change the arg, it's in the right
  band. Bump `invocations:` to ≥10 and re-measure.

### Tier 4 — borderline, low priority

- [ ] **`devkit_gzip` — flagged but possibly correct**

  Data: wall **2.5s**, GC **0.7%**, minor 474 / major 228, IQR 1.4%.

  NO_GC_PRESSURE flagged because GC% is below 1%. But for a gzip-style
  compute-bound benchmark, that's expected behaviour — not all benchmarks
  should exercise the GC. The earlier 47% wall regression we saw in
  Notebook A §4 is therefore a *real compute regression* in the new
  compiler, not a GC story.

  Action: leave the input alone; tag this benchmark explicitly as
  "compute-bound, GC-irrelevant" so the NO_GC_PRESSURE flag becomes a
  no-op. Possible mechanism: add a `tags:` field per benchmark in the
  YAML and have the health view skip the GC-pressure check on entries
  tagged `compute-bound`.

  Files: `macrobenchmarks_base.yml` (add tag), notebook health view
  (respect tag).

## In envelope — leave alone

These 16 benchmarks are within all envelope thresholds in this dataset:
`alt_ergo_unsat_smt2`, `alt_ergo_yyll`, `cpdf_blacktext`, `cpdf_merge`,
`cpdf_scale`, `cpdf_squeeze`, `devkit_htmlstream`, `devkit_network`,
`eio_fiber_stream`, `irmin_mem_rw`, `menhir_sql_parser`, `menhir_sysver`,
`ocamlformat_rocq`, `sedlex_tokenize`, `test_decompress`, `ydump_repeat`.

They may still drift later (bigger compilers, different inputs, changed
runtime defaults) — re-run the health view after any meaningful change.

## Workflow

For each Tier 1/2 entry:

1. Read the relevant build script and any input file referenced.
2. Decide on the change (smaller/larger input, OCAMLRUNPARAM tweak,
   re-architect).
3. Make the change in `~/macro-benches/...`, commit it there.
4. Re-run **just that benchmark** — minimal YAML override is the fastest
   path:
   ```yaml
   includes: ["./macrobenchmarks_base.yml"]
   overrides:
     invocations: 5     # or more, for variance-sensitive cases
     benchmarks:
       <suite-name>:
         - <benchmark-name>
   runtimes:
     ocaml-5.4.1: { type: OCaml, version: "5.4.1" }
   configs:
     - "ocaml-5.4.1|perf_grp1|re-23|md-2"
   ```
5. Verify wall / GC% / IQR are now in envelope using Notebook B's health
   view against the new log dir (set `BENCH_LOGS_DIR=...`).
6. Tick the box, commit the YAML change, move on.

## Cross-cutting finding: shell-wrapper observability

Several benchmarks use a generated shell wrapper that runs the OCaml
binary `N` times in a `for` loop:

```bash
ITERATIONS="${1:-N}"
for _ in $(seq 1 "$ITERATIONS"); do
  "${REAL_EXE}" >/dev/null 2>&1
done
```

This pattern interacts badly with olly's runtime_events attach model
(one OCaml process at a time). Empirically:

- **Per-child runtime ≪ wrapper wall**: olly aggregates, sometimes
  correctly. `devkit_stre` (per-child 6 ms, 80 children) and
  `owl_gc` (per-child 0.27 s, 7 children) report olly_wall_time
  matching the wrapper's real wall.
- **Per-child runtime ≈ wrapper wall**: olly attaches to one child
  and reports its lifetime. `pplacer_testsuite` (per-child 4 s,
  8 children) reports olly_wall = 4 s while the wrapper's real wall
  is ≈ 32 s. **Other 7 children unobserved.**

The exact threshold isn't documented and depends on olly's polling
behaviour, but the practical implications are:

1. The dataset's wall times for shell-wrapped benchmarks may be
   per-child, not whole-wrapper. The notebook's health view inherits
   this confusion.
2. **The fix shape, validated on `pplacer_testsuite`:** push the
   iteration loop *inside* the OCaml binary via an env var, drop the
   shell `for` loop, and let olly attach to a single observable
   process. See `macro-benches/README.md` §"Iteration counts" for
   the canonical pattern + checklist.
3. For `devkit_stre`/`gzip`/`network` and `owl_gc` the same fix
   applies — they all need an env-var-driven in-process loop in
   their respective `*_bench.ml` files.

Affected benchmarks (use shell-loop wrapper):
`pplacer_testsuite` (fixed), `devkit_stre`, `devkit_gzip`,
`devkit_network`, `owl_gc`.

Unaffected (direct exec):
`zarith_pi`, `liq_parse_typecheck`, `coqc_corelib_stress`,
`alt_ergo_*`, `cpdf_*`, `irmin_mem_rw`, `menhir_*`,
`ocamlformat_rocq`, `sedlex_tokenize`, `test_decompress`,
`ydump_repeat`, `eio_fiber_stream`, `devkit_htmlstream`.

## What this list is *not*

- **Not a noise-isolation list.** Issue 3 in
  [benchmark-noise-and-comparison-plan.md](./benchmark-noise-and-comparison-plan.md)
  covers the system-side fixes (taskset, drop_caches, frequency lock).
  Once those land we re-run the health view — some HIGH_VARIANCE
  flags will likely clear themselves up.
- **Not a regression-tracking list.** Notebook A's per-comparison view
  is the place to look at "did this PR regress this benchmark". The
  envelope tells us *which benchmarks we can trust* the regression
  signal from.
- **Not a benchmark-suite curation list.** Whether a benchmark
  *should be in the suite* is a separate question (covered by the
  proposal's macrobenchmark category list). This doc is "given that
  it's in the suite, get it into a usable shape".
