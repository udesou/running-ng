# Adding an experiment

An experiment consists of a YAML file (configuration) under
`src/running/config/experiments/`. The configuration contains a section to
include (`includes:`) a base configuration, declaring only `runtimes`,
`configs`, `modifiers`, `config_sweep`, `comparisons` and `overrides`, as
described in [configs.md](configs.md).

## Start from a baseline

Four configurations under `src/running/config/examples/` cover the shapes most
experiments take. Copy the closest one into `experiments/` and edit it.

| Baseline | What it runs |
|---|---|
| [`baseline_micro.yml`](../../src/running/config/examples/baseline_micro.yml) | every micro benchmark on the current stable compiler |
| [`baseline_macro.yml`](../../src/running/config/examples/baseline_macro.yml) | every macro benchmark on the current stable compiler |
| [`baseline_variants.yml`](../../src/running/config/examples/baseline_variants.yml) | one compiler built two ways (stock and frame pointers + flambda) |
| [`baseline_sweep.yml`](../../src/running/config/examples/baseline_sweep.yml) | one compiler over a grid of GC parameters |

## What a configuration says

Reading `baseline_variants.yml` top to bottom, block by block.

```yaml
includes:
  - "../base/ocaml/macro_base.yml"
```

Specifies which benchmarks exist and how they are measured. The base defines the complete suites, the
benchmark list, the modifiers and the `tags:` block. Including `macro_base.yml`
selects the macro suite, `micro_base.yml` the micro one with everything below being
defined over that base set of benchmarks.

```yaml
overrides:
  invocations: 3
```

Specifies how many times each cell runs. `invocations` is a scalar the base already sets,
so it changes through `overrides:` (defining outside `overrides` produces a type error).

```yaml
runtimes:
  ocaml-5.5.1:
    type: OCaml
    version: "5.5.1"
  ocaml-5.5.1-fp-flambda:
    type: OCaml
    version: "5.5.1"
    configure_args: ["--enable-frame-pointers", "--enable-flambda"]
```

Specifies the compilers to be tested. Each one can be built from a git ref (`version:` for a release tag,
`commit:` for a sha) into its own opam switch, `running-ng-<name>`. Here both
entries share one source and differ only in `configure_args`, which is what
makes this a variants experiment. The runtime *name* uniquely tells the builds apart, naming the switch 
and each the benchmark binary (`<bench>-<name>`) and the series in the results. Adding a third variant is one
more entry plus one more config string, which `experiments/macro_fp_flambda_5.5.1.yml` defines.

```yaml
configs:
  - "ocaml-5.5.1|perf_grp1"
  - "ocaml-5.5.1-fp-flambda|perf_grp1"
```

Determines which cells to run: one string per cell, a runtime followed by the modifiers
applied to it. `perf_grp1` attaches `perf` and `olly` to the benchmark process,
producing the metrics for each tool. A modifier that takes a
value carries it after a dash (`s-131072`). Every runtime declared above must
appear in some config string.

```yaml
comparisons:
  - label: "5.5.1: fp+flambda vs stock"
    a: ocaml-5.5.1
    b: [ocaml-5.5.1-fp-flambda]
```

Defines which runtimes are meant to be read against which, so the dashboard knows what
to plot. `a` is the baseline and `b` the candidates. `b` is a list because
one baseline may be compared against multiple candidates. 
This does not define a filter on what runs, it simply records metadata for what gets visualized in
the dashboards.

### Sweeping a parameter

`baseline_sweep.yml` exercises a parameter sweep over a single runtime:

```yaml
config_sweep:
  s: [131072, 262144, 524288]
  o: [80, 120, 200]
```

Each key names a modifier from the base (`s` is the minor heap in words, `o` the
space overhead percentage) and each value is cross-producted into every config
string that does not already fix that key. One config string here becomes nine
cells, `ocaml-5.5.1|perf_grp1|re-25|md-2|s-131072|o-80` and so on, each with its
own `OCAMLRUNPARAM`. Multiply by invocations and by benchmarks before adding a
row.

That file also narrows the benchmark set:

```yaml
overrides:
  benchmarks:
    sandmark-sequential: [bdd, kb, lexifi-g2pp, minilight]
```

Under `overrides:` this **replaces** the base's benchmark set. Adding a block without `overrides` would *extend* it instead, which for a sweep means running the grid over everything.

## Running it

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/<your>.yml \
LOG_DIR=$PWD/gc-sweep-logs-<your>-$(date +%F) \
  bash run_ocaml_bench_gc_sweep.sh
```

## Gotchas

- **Only override through `overrides:`.** Redeclaring a scalar the base already
  sets at top level is a `TypeError` at load time, and a top-level `benchmarks:`
  block *extends* the base's rather than replacing it.
- **Every runtime must appear in `configs:`**, and, if you declare
  `comparisons:`, in a comparison too. Dead declarations and uncovered runtimes
  are validation errors, not warnings.
