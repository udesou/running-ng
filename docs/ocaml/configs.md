# What a config consists of

Configs live under `src/running/config/`:

```text
base/ocaml/micro_base.yml   suites + benchmarks + modifiers for ~/benches
base/ocaml/macro_base.yml   ditto for ~/macro-benches, plus the runtime-feature `tags:` block
examples/                   the baseline configs to copy from, plus smoke tests
experiments/                one file per real experiment
*/upstream/                 the JVM/DaCapo lineage, not applicable to OCaml
```

**Build on top of defined configs.** An experiment configuration `includes:` a base and declares only
what is specific to it: `runtimes`, `configs`, `modifiers`, `config_sweep`,
`comparisons`, `invocations`. The base owns the suites, the benchmark lists and
the shared modifiers, so a change there propagates to every experiment.

## The five blocks

**`runtimes:`** which compilers. See [runtimes.md](runtimes.md).

**`suites:` + `benchmarks:`** `suites` declares the available programs (path,
args, timeout, build script); `benchmarks` selects which ones actually run. Both
normally come from the base. Suite types:

| Type | Behaviour |
|---|---|
| `OCamlBenchmarkSuite` | builds via the benchmark's build script, runs the binary |
| `OCamlMulticoreBenchmarkSuite` | same, but fails if the runtime is OCaml < 5 |
| `OCamlOxcamlBenchmarkSuite` | same, but fails unless the runtime is `type: OxCaml` (for `Domain.Safe`, prefetch intrinsics, ...) |
| `OCamlMacroBenchmarkSuite` | per-benchmark *satellite* opam switches. Not used by the current macro path, which is the vendored monorepo under a plain `OCamlBenchmarkSuite`. |

Paths use `${RUNNING_BENCH_DIR}` (micro) or `${RUNNING_MACRO_BENCH_DIR}` (macro),
expanded from the environment at load time.

**`modifiers:`** how to tweak a run. See [modifiers.md](modifiers.md).

**`configs:`** the cells to run, one string each:
`"<runtime>|<modifier>|<modifier>|..."`. A modifier takes a value with a dash:
`re-25` applies the `re` modifier with value `25`.

```yaml
configs:
  - "ocaml-5.4.1|perf_grp1"
  - "ocaml-d8bb46c|perf_grp1"
```

**`config_sweep:`** cross-product expansion. For each base config string, any
sweep key **not already present** in that string is expanded over its values:

```yaml
config_sweep:
  s: [131072, 262144, 524288, 1048576, 2097152]
  o: [40, 80, 120, 150, 200]
```

turns each config above into 5 x 5 = 25 cells
(`ocaml-5.4.1|perf_grp1|s-131072|o-40`, ...). Each `s-131072` expands the modifier
template `s={0}` into `s=131072`, concatenated into `OCAMLRUNPARAM`.

## `comparisons:` and `schema_version:`

`comparisons:` declares which runtimes are meant to be compared, so downstream
tooling (the dashboard) knows what to plot:

```yaml
comparisons:
  - label: "version effect"
    a: ocaml-5.4.1
    b: ocaml-d8bb46c
    # mode: pairwise (default) | cartesian; a/b may be lists
```

Configs are validated before anything runs: unknown runtimes, runtimes declared
but never used, runtimes compared but never run, and malformed comparison blocks
are all hard errors.
