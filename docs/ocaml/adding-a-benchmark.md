# Adding a benchmark

Benchmarks should be added in a particular repository according to what the benchmark covers. If the benchmark:

- is a self-contained program structured to stress a particular runtime feature, add it to [**benches**](https://github.com/ocaml-bench/benches)
- is a real-world application with a dependency tree, add it to [**macro-benches**](https://github.com/ocaml-bench/macro-benches), which
  vendors every dependency so all runtimes compile identical source

Please open a PR against each repo to add the benchmark you would like to run. After adding the benchmark, it needs to be registered inside running-ng.

## 1. Write the benchmark and its build script

The current infrastructure allows running-ng to activate the runtime's opam switch (with the compiler, `dune` and installed
packages on `PATH`) and then run `<name>.build.sh` in the benchmark directory, passing four environment variables (predefined by the benchmark configuration):

| Variable | Meaning | Fallback when unset |
|---|---|---|
| `RUNNING_OCAML_BENCH_DIR` | directory holding this benchmark's sources | the script's own directory |
| `RUNNING_OCAML_OUTPUT` | path the built binary **must** be written to | `${BENCH_DIR}/<name>-${RUNTIME_NAME}` |
| `RUNNING_OCAML_RUNTIME_NAME` | runtime identifier, e.g. `ocaml-5.4.1` | `runtime` |
| `RUNNING_OCAML_SWITCH` | opam switch name, when there is one | unset |

See the examples inside [**benches**](https://github.com/ocaml-bench/benches) and [**macro-benches**](https://github.com/ocaml-bench/macro-benches) on how to 
write your own `<name>.build.sh` file.

## 2. Register it in a suite

Add the program to the right suite in `base/ocaml/micro_base.yml` or
`base/ocaml/macro_base.yml`, then enable it in the same file's `benchmarks:`
block:

```yaml
suites:
  macro-<tool>:
    type: OCamlBenchmarkSuite      # OCamlMulticoreBenchmarkSuite if it needs OCaml 5
    timeout: 600                    # seconds, per invocation
    programs:
      <name>:
        path: "${RUNNING_MACRO_BENCH_DIR}/benchmarks/<tool>"
        build_script: "<tool>.build.sh"    # optional: defaults to <name>.build.sh
        args: "..."                         # optional: run-time arguments
        # binary: "<tool>-{runtime}"        # optional: defaults to <name>-<runtime>
        # build_env: { FOO: bar }           # optional: extra build-time env
        # always_build: true                # optional: rebuild even if the binary exists
        # ocamlrunparam: "e=25,d=2"         # optional: per-benchmark ring size
        # expected_exit: 142                # optional: exit code a *successful* run returns to indentify potential failures

benchmarks:
  macro-<tool>:
    - <name>
```

Micro benchmarks follow a similar structure. See `base/ocaml/micro_base.yml` for an example.

## 3. Tag it (macro only)

You might want to tag your benchmark if it exercises a particular feature in the runtime
or fits the same input size category as the other macro-benchmarks. For more information see [tags.md](tags.md).

## 4. Verify

To verify if the benchmark builds and runs, point a small config at it (e.g., copy `examples/smoke_macro.yml` and narrow
`overrides.benchmarks` to your suite), then:

```bash
# builds only: every runtime in the config, no measurement
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/examples/<your-smoke>.yml \
  bash build_ocaml_binaries_gc_sweep.sh

# then one invocation end-to-end
RUNNING_MACRO_BENCH_DIR=~/macro-benches \
CONFIG_FILE=src/running/config/examples/<your-smoke>.yml \
LOG_DIR=/tmp/newbench \
  bash run_ocaml_bench_gc_sweep.sh
```

Make sure to check the `olly_*.json` sidecar for a plausible `wall_time` and no lost events. If
events were lost, raise the benchmark's `ocamlrunparam` ring size (and keep `d=`
at the real domain count).

Finally, document it: benches and macro-benches each keep a per-benchmark
description.
