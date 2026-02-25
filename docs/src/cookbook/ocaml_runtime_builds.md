# OCaml Runtime Builds And Benchmark Binaries

This page documents the OCaml workflow added to `running-ng`:

1. pick runtime from `configs`,
2. build/cache compiler runtime when needed,
3. build/cache benchmark binary for that runtime,
4. run benchmark with modifiers.

## Runtime Selection And Build

`configs` still selects runtime by name:

```yaml
configs:
  - "ocaml-release|time_stats|d-1|s-262144|o-10|i-64|a-2"
```

For `OCaml` runtimes, supported keys are:

- `executable`: use existing `ocaml` binary directly.
- `version`: build OCaml from that version (for example `5.4.0`) if not cached.
- `commit` or `hash`: build OCaml from that Git commit if not cached.

Example:

```yaml
runtimes:
  ocaml-local:
    type: OCaml
    executable: /home/udesou/.local/ocaml/bin/ocaml

  ocaml-v5_4_0:
    type: OCaml
    version: "5.4.0"

  ocaml-commit:
    type: OCaml
    hash: "02ee646ee1f40eb19f4942f50a4a607b52b3ab39"
```

Optional runtime build keys:

- `repo` (default: `https://github.com/ocaml/ocaml.git`)
- `cache_dir` (default under `/tmp/running-ng-ocaml-toolchains`)
- `configure_args` (list)
- `make_targets` (list, default `["world.opt"]`)
- `jobs` (parallel make jobs)

## OCaml Benchmark Build Convention

For `OCamlBenchmarkSuite`, if program `path` is a directory, `running-ng` uses build mode.

Defaults:

- build script: `<benchmark-name>.build.sh`
- output binary: `<benchmark-name>-<runtime-name>`

So for benchmark `binarytrees` and runtime `ocaml-local`, default binary is:

- `binarytrees-ocaml-local`

Example suite:

```yaml
suites:
  ocaml-binarytrees:
    type: OCamlBenchmarkSuite
    timeout: 120
    programs:
      binarytrees:
        path: /home/udesou/benches/binarytrees
        args: "21"
```

You can override convention if needed:

```yaml
programs:
  binarytrees:
    path: /home/udesou/benches/binarytrees
    build_script: custom.build.sh
    binary: custom-binary-{runtime}
```

`binary` supports formatting keys `{benchmark}` and `{runtime}`.

## Rebuild Policy

Default behavior:

- if binary already exists: skip build and log a warning,
- else: run build script.

To force rebuild every run:

```yaml
suites:
  ocaml-binarytrees:
    type: OCamlBenchmarkSuite
    always_build: true
```

Or per benchmark:

```yaml
programs:
  binarytrees:
    path: /home/udesou/benches/binarytrees
    always_build: true
```

## Build Script Environment

`running-ng` sets these variables when invoking build scripts:

- `OCAML_EXECUTABLE`: selected runtime executable path.
- `OCAML_HOME`: runtime prefix (`.../bin/..`).
- `RUNNING_OCAML_OUTPUT`: expected output binary path.
- `RUNNING_OCAML_BENCH_DIR`: benchmark directory (`program.path`).
- `RUNNING_OCAML_RUNTIME_NAME`: runtime name from config.

Your build script should place the final executable at `RUNNING_OCAML_OUTPUT`.

## End-To-End Summary

For each `configs` entry:

1. resolve runtime,
2. build runtime if needed (version/commit/hash modes),
3. ensure benchmark binary exists for that runtime (or rebuild if `always_build`),
4. run benchmark with selected modifiers.

