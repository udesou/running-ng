# Macro-benchmark coverage gaps — closing plan

Plan for closing the diagnostic blind spots identified in the
April 2026 calibration session. The list of gaps lives in
`~/macro-benches/README.md` §"Coverage gaps". This doc is the
*action* side: how to close each gap with a real OCaml application
workload (not a synthetic micro-benchmark), in priority order.

## Context and scope

The macro-bench suite, after the April calibration pass, has 24
working benchmarks that exercise allocation patterns, GC behaviour,
FFI, effects (Eio), and Lwt scheduling. Several runtime areas are
*not* exercised by any current benchmark — a regression touching
those would slip through silently.

**Approach guideline.** These are macro-benchmarks: they should be
real OCaml applications doing the work as a primary mode, not as a
side effect. Where a real application doesn't exist (or is too
specialised — e.g. `Gc.alarm`), we accept the gap rather than fake
it with a synthetic kernel.

## Phase 1 — `ocamlc_self_compile`  *(ready to start)*

Closes: **Ephemeron**, **Marshal**, replaces flawed `dune_bootstrap`
diagnosis surface. Single observable OCaml process — olly sees
everything.

### Why this benchmark

The OCaml compiler itself is the canonical real-world user of
ephemeron-keyed tables: `typing/btype.ml` and friends use them for
type hash-consing during type inference, and the Hashtbl-based caches
are scattered through `parsing/`, `typing/`, and `bytecomp/`.
Compilation also produces `.cmi` (typed-AST) and `.cmo`/`.cmx` files
via `Marshal`, exercising the marshal path on real data.

We currently have `dune_bootstrap` in this conceptual slot but it
spawns subprocesses and the parent we measure does almost no work.
`ocamlc -c <bigfile>.ml` is a single-process, fully observable
compiler-throughput benchmark.

### Decision: replace or augment `dune_bootstrap`?

**Recommend augment.** Reasons:

- `dune_bootstrap` is a real-world end-to-end metric: how long does
  it take dune to bootstrap from source on this machine? That is
  what users experience and is intrinsically valuable, even if the
  parent's runtime stats are noise.
- `ocamlc_self_compile` is a different angle: it isolates compiler
  internals in one process, which the `dune_bootstrap` cross-process
  setup cannot do.
- Adding a `tag: external-work` to `dune_bootstrap` (when the tag
  mechanism lands — see calibration triage doc) makes its
  observability story explicit instead of broken.

Keep both. They answer different questions.

### Workload — what to compile

Three candidate inputs, in increasing order of work and complexity to
set up. Pick one in execution.

**Option A — vendored OCaml stdlib + parsing.** Concatenate a known
set of modules from a pinned OCaml release tarball into one big file,
compile with `ocamlc -c`. Pros: stable across compiler versions
(stdlib doesn't change much), bounded size, easy to vendor. Cons:
stdlib is small (~few thousand lines), so compile time is short.

**Option B — `ppxlib` source.** ppxlib has substantial type-heavy
OCaml code, exercises GADTs, polymorphic types, and triggers heavy
type-inference work. Pros: real-world library used everywhere,
already in our duniverse. Cons: relies on having ppxlib's deps
available; mixing AST flavours across compiler versions is fragile.

**Option C — a self-contained concatenation of OCaml's typing
modules.** Take `parsing/parsetree.ml` + `parsing/asttypes.ml` +
`typing/types.ml` + `typing/btype.ml` + `typing/ctype.ml` (or a
reasonable subset that compiles standalone), concatenate, compile.
Pros: explicitly exercises the parts using ephemerons. Cons: most
fragile across versions; module order matters; the compiler's own
modules have implicit dependencies on `compiler-libs`.

**Recommendation: start with Option A.** Cleanest to build and
maintain. If the workload is too short on this hardware, escalate to
Option B. Option C is only worth doing if we want to specifically
hit the type-hashcons code path and the others don't.

### Build script outline

```
benchmarks/ocamlc-self-compile/
  ocamlc-self-compile.build.sh
  inputs/
    big_module.ml        # concatenated source (gitignored if regenerable)
    big_module.mli       # optional, depends on the chosen input
```

Build script responsibilities:

1. **Generate** `inputs/big_module.ml` from the chosen source (vendored
   stdlib for Option A). Idempotent; runs only when source changes.
   Same pattern as `alt-ergo.build.sh` does for `fill_x100.why`.
2. **Locate** the right `ocamlc` for the runtime. The runtime under
   test *is* the compiler, so use the runtime's own `bin/ocamlc.opt`
   (or `bin/ocamlc` if the opt variant isn't available — relevant
   for OxCaml).
3. **Generate the wrapper** at `${OUT}` — a small bash wrapper that
   invokes `ocamlc.opt -c inputs/big_module.ml -o /tmp/...`. Single
   process, no shell loop needed (the compile is already long enough).
   Strip output dir before/after to keep the run reproducible.

### YAML entry

```yaml
ocamlc-self-compile:
  type: OCamlBenchmarkSuite
  timeout: 300
  programs:
    ocamlc_self_compile:
      path: "${RUNNING_MACRO_BENCH_DIR}/benchmarks/ocamlc-self-compile"
      build_script: "ocamlc-self-compile.build.sh"
      args: ""
```

Add to `benchmarks:` block of `macrobenchmarks_base.yml`.

### Verification

After build:

1. Run solo with `/usr/bin/time` — confirm wall is in 10-30s range
   on this machine (pick input size to land here; bump if too short).
2. Run with olly — expect substantial minor *and* major collection
   counts (compiler does both), gc_overhead in 15-40% range, RSS in
   the 100-500 MB range depending on input size.
3. Add to `comparisons:` block of
   `fp_flambda_macro_5.4.1_vs_d8bb46c.yml` (probably as one of the
   per-flag-combo or version-effect pairs).
4. Re-run notebook B health view — `ocamlc_self_compile` should be
   in envelope; flag any deviation.

### Critical files to modify/create

In `~/macro-benches`:

- `benchmarks/ocamlc-self-compile/ocamlc-self-compile.build.sh`  *(new)*
- `benchmarks/ocamlc-self-compile/inputs/big_module.ml`  *(generated, gitignored)*
- `dune-project` — add a package declaration if the input has
  build-time deps.
- `.gitignore` — `benchmarks/ocamlc-self-compile/inputs/big_*` if
  generated.
- `README.md` — extend §"Benchmark characteristics" with a new
  entry; tick the Ephemeron / Marshal items in §"Coverage gaps".

In `~/running-ng`:

- `src/running/config/macrobenchmarks_base.yml` — new
  `macro-ocamlc-self-compile` suite entry.
- `src/running/config/fp_flambda_macro_5.4.1_vs_d8bb46c.yml` — add
  `ocamlc_self_compile` to relevant `comparisons:` blocks.
- `docs/benchmark-coverage-gaps-plan.md` (this file) — tick the
  Phase 1 box.

### What "done" looks like

Phase 1 is done when:

- [ ] Build script generates the input and produces a working wrapper
      for at least `ocaml-5.4.1` and `ocaml-d8bb46c` runtimes.
- [ ] Solo wall in 10-30s range; olly observes the full run with no
      lost events; gc_overhead in 15-40%, with both minor and major
      collections in the thousands.
- [ ] Macro-benches commit lands with the build script, generator,
      gitignore, and characteristics entry.
- [ ] running-ng commit lands with YAML suite + comparisons entry.
- [ ] One full run of `fp_flambda_macro_5.4.1_vs_d8bb46c.yml`
      including the new benchmark; numbers ingested into the
      notebook; nothing flagged red.

## Phase 2 — Sandmark imports  *(pending manager approval)*

Sandmark is the established OCaml runtime benchmark suite; many of
its programs were *built* to test specific runtime areas. Promoting
two or three of them into our macro-bench monorepo would close the
multicore and flambda gaps with workloads the runtime team already
trusts.

### Candidates (pick 2–3)

**`parallel_binarytrees`** — closes multi-domain gap.

The classic binarytrees benchmark distributed across N domains via
`Domainslib`. Real workload structurally (trees + traversal); also
the canonical multicore stress test that the runtime team uses.

Sensitive to: `Domain.spawn`, `Atomic`, inter-domain GC,
domain-local minor heaps under contention. None of which any
existing benchmark touches.

**`LU_decomposition_multicore`** — closes parallel-numerical gap.

LU matrix decomposition with `parallel_for`. Float-heavy parallel
work. Fills both the multi-domain and the float-hot-loop gaps with
one workload (although it's less pure-flambda than a single-domain
ray tracer).

**`raytracer`** — closes flambda hot-loop gap.

Single-domain pure-OCaml ray tracer. Float-heavy inner loop with no
FFI; flambda has plenty to optimise.

**`nbody`** — closes float-hot-loop gap (alternative to raytracer).

N-body gravitational simulation. Pure OCaml floats. Sandmark has
both single-threaded and multicore variants.

### Approach when imports happen

For each Sandmark benchmark:

1. Vendor its source into `~/macro-benches/benchmarks/sandmark-<name>/`.
2. Write a `<name>.build.sh` that wraps the existing Sandmark build
   logic (most are dune-buildable directly).
3. If iteration count needs scaling, apply the same env-var or
   Sys.argv pattern as `pplacer_testsuite` / `owl_gc` (see
   `~/macro-benches/README.md` §"Iteration counts").
4. YAML entry, characteristics doc, comparisons block, run, verify.

The mechanical work is straightforward; the question is selection
(which 2-3 of the 4 candidates). That's the manager check.

### Decision matrix to discuss

| Candidate | Gap closed | Risk |
|---|---|---|
| `parallel_binarytrees` | multi-domain, atomic, inter-domain GC | most stable; OCaml multicore team blesses it |
| `LU_decomposition_multicore` | multi-domain + parallel-numerical | also blessed; requires libgsl-style numerical setup |
| `raytracer` | flambda hot-loop, single-domain float | low risk; pure OCaml |
| `nbody` | flambda hot-loop, can be either | low risk; pure OCaml |

Recommend `parallel_binarytrees` + `raytracer` as the minimal pair —
covers multicore + flambda with two distinct workloads.
`LU_decomposition_multicore` is a stretch goal if the manager wants
parallel-numerical specifically.

## Phase 3 — Bigarray slicing/reshape patterns

Lower priority. Owl already exercises Bigarray broadly. Could *extend*
`owl_gc.ml` to do more slicing-heavy patterns rather than add a
separate benchmark. Defer until we've seen evidence that Bigarray
slicing is a real regression target.

## Out of scope (gaps we're accepting)

- **`Gc.alarm` callbacks** — used by `memprof-limits` and a few
  profiling tools; not a macro workload by nature. Best covered by a
  micro in `~/benches/` if anyone cares.
- **Polling-points / safe-points** — mostly an Eio-internal concern
  and intertwined with the effect handler. Hard to isolate as a
  macro.
- **`Sys.set_signal` in tight loops** — niche.

If a runtime change touches one of those areas, **flag it
explicitly in the PR description**: "this touches an area not
exercised by any benchmark; expect no regression signal in the
macro suite". The macro suite catches what it catches; we don't
fake coverage.

## Recommended order

1. **Phase 1 first** — `ocamlc_self_compile` is concrete, ready,
   and the win-to-effort ratio is high (closes two gaps + improves
   on `dune_bootstrap`).
2. **Discuss Phase 2 with manager** — sanity-check that promoting
   from Sandmark is acceptable, pick the 2–3 to start with.
3. **Phase 2 implementation** — once approved.
4. **Phase 3** — if we observe a regression that would have been
   caught by it.

## What this plan does NOT do

- **Doesn't write benchmarks for niche runtime features** (Gc.alarm
  etc.). Coverage gaps remain on those by design.
- **Doesn't reshape existing benchmarks** beyond the calibration
  pass already done.
- **Doesn't address the `tag:` mechanism** for the health view —
  separate work tracked in `benchmark-calibration-triage.md`.
- **Doesn't replace `dune_bootstrap`** — augments, per the recommendation
  above.
