# OCaml benchmarking with running-ng

 This file contains an index for the other documents that are pertinent to OCaml support inside running-ng. Start from the [README](../../README.md) if you need a quick summary beforehand.

| Document | Covers |
|---|---|
| [running.md](running.md) | the launch scripts, the subcommands, the environment variables, and what a run writes out |
| [configs.md](configs.md) | what a config consists of: layering, the five blocks, sweeps, comparisons |
| [runtimes.md](runtimes.md) | declaring compilers: OCaml, OxCaml, MMTk, and how switches are provisioned |
| [modifiers.md](modifiers.md) | GC knobs, hardware counters, CPU pinning, runtime-events ring sizing, allocation tracing |
| [tags.md](tags.md) | selecting benchmarks by runtime-feature tag (`RUNNING_TAG`) |
| [adding-a-benchmark.md](adding-a-benchmark.md) | the build-script contract and how to register a new program |
| [adding-an-experiment.md](adding-an-experiment.md) | writing an experiment file and keeping the grid affordable |

The rest of `docs/` is upstream's mdBook (JVM oriented), this fork's benchmark
methodology notes, and [freebsd-support.md](../freebsd-support.md).
