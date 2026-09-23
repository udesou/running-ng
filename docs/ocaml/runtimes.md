# Runtimes

Each entry in `runtimes:` names a compiler. Non-`executable` runtimes are built by
`opam compiler create` into a switch named `running-ng-<runtime-name>`.

```yaml
runtimes:
  ocaml-5.4.1:                 # by release tag
    type: OCaml
    version: "5.4.1"
  ocaml-d8bb46c:               # by commit
    type: OCaml
    commit: "d8bb46c39bf5fcafb513a8ba18e667d3f8c2600a"
  ocaml-5.4.1-fp-flambda:      # same source, different configure
    type: OCaml
    version: "5.4.1"
    configure_args: ["--enable-frame-pointers", "--enable-flambda"]
  ocaml-local:                 # a compiler you built yourself
    type: OCaml
    executable: "/path/to/bin/ocaml"
```

`repo:` selects a fork (default `https://github.com/ocaml/ocaml.git`); it must be
a GitHub URL, since `opam-compiler` resolves `user/repo:ref`. Use either
`version:` or `commit:`, not both. `dune_version:` pins dune for one runtime.

## Switch provisioning

**A switch left over from an earlier run is removed and rebuilt by default**, so
the compiler and the pinned dune are what this run provisioned rather than
whatever a previous run installed. For long sweeps over switches you trust set `RUNNING_REUSE_SWITCHES=1`; reuse mode
still refuses a switch whose compiler never finished building. 

Two runs sharing an opam root are refused outright (`$OPAMROOT/running-ng.lock`), because one would
delete a switch the other is using, for that reason make sure to give concurrent campaigns separate
`OPAMROOT`s. Whatever switch
was active before the run is restored afterwards, even on a crash. 

## OxCaml

```yaml
oxcaml-trunk:
  type: OxCaml
  commit: "<sha>"
  configure_args: ["--enable-poll-insertion", "--enable-multidomain"]
```

Handles OxCaml's different build system (autoconf, `--enable-runtime5`, Dune-based
`make install`) and builds a stock bootstrap compiler if needed
(`bootstrap_version`, default `5.4.0`). Default repo is
`https://github.com/oxcaml/oxcaml.git`. Source checkouts are cached under
`/tmp/running-ng-ocaml-toolchains/`, and the build gets its own opam switch
(`running-ng-oxcaml-build`).

**Multicore on OxCaml requires both `--enable-poll-insertion` and
`--enable-multidomain`**; without them domain creation fails at run time with
`failed to allocate domain`.

## MMTk

`type: OCamlMMTk` runs on [ocaml-mmtk](https://github.com/fplaunchpad/ocaml-mmtk),
OCaml 5.5 with [MMTk](https://www.mmtk.io/) in place of the stock collector. It is
a drop-in: the same launch scripts work unchanged.

```yaml
runtimes:
  ocaml-mmtk:
    type: OCamlMMTk
    commit: "cbc66e3efd8f9200f3e84f791a6b6dfc36efce8c"
    # repo: defaults to fplaunchpad/ocaml-mmtk; set it to build another fork
```

The only extra prerequisite is **Rust/cargo at `~/.cargo`**: MMTk's static library
is built by cargo inside the compiler's `make`. Everything else is automatic (ASLR
disabled for every MMTk command, a fixed build-time heap, a `LIBRARY_PATH` that
lets dune-configurator probes link `-lmmtk_ocaml`, and a temporarily relaxed opam
sandbox so cargo can fetch crates). 

Plan and heap are run-time environment variables, supplied as modifiers:

| Env var | Meaning |
|---|---|
| `MMTK_PLAN` | `Immix` (default) · `StickyImmix` · `GenImmix` · `MarkSweep` · `NoGC`. **Native code needs an Immix-family plan.** |
| `MMTK_HEAP_SIZE_MB` | **Fixed** heap size (a hard bound, unlike stock OCaml's soft target) |
| `MMTK_THREADS` | GC worker threads |
| `MMTK_VERBOSE` | print `[mmtk]` init line and exit-time GC stats |

`macro_base.yml` ships `plan` and `threads` as name-value modifiers, so
`|plan-StickyImmix|threads-4` both sets the variables and records them as distinct
dimensions in the contract:

```bash
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/experiments/mmtk_macro.yml \
  bash run_ocaml_bench_gc_sweep.sh
```

Because `MMTK_HEAP_SIZE_MB` is a hard bound, the `minheap` subcommand can
binary-search the smallest heap each benchmark completes in; MMTk is the only
runtime that supports it.
