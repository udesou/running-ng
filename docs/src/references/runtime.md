# Runtime

## `JikesRVM`

## `NativeExecutable` (preview ⚠️)
A `NativeExecutable` type specifies [`runbms`](../commands/runbms.md) to
directly run the benchmarks on native hardware. This is supposed to be used in
tandem with
[`BinaryBenchmarkSuite`](./suite.md#BinaryBenchmarkSuite).

## `OpenJDK`

## `D8` (preview ⚠️)
### Keys
`executable`: path to the `d8` executable.

## `SpiderMonkey` (preview ⚠️)
### Keys
`executable`: path to the `js` executable.

## `JavaScriptCore` (preview ⚠️)
### Keys
`executable`: path to the `jsc` executable.

## `OCaml` (preview ⚠️)
### Keys
`executable`: path to the OCaml runtime executable, such as `ocaml` or `ocamlrun`.
