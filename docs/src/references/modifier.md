# Modifier

## `EnvVar`
### Keys
`var`: name of the variable.

`val`: value of the variable.

### Description
Set an environment variable. Might override an environment variable inherited from the parent process.

## `JVMArg`
`JVM` specific.
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple arguments are space separated.

### Description
Specify arguments to a JVM, as opposed to the program.

## `JSArg` (preview ⚠️)
`JavaScriptRuntime` specific.
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple arguments are space separated.

### Description
Specify arguments to a JavaScript runtime (e.g., `d8`), as opposed to the program.

## `OCamlArg` (preview ⚠️)
`OCaml` specific.
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple arguments are space separated.

### Description
Specify arguments to an OCaml runtime (e.g., `-domain-count 4`), as opposed to the program.

## `OCamlRunParam` (preview ⚠️)
`OCaml` specific.
### Keys
`val`: a raw `OCAMLRUNPARAM` fragment (for example, `s=262144`).

### Description
Append one fragment to `OCAMLRUNPARAM`.
Multiple `OCamlRunParam` modifiers are joined with commas in the order they appear in a config string.

## `JVMClasspathAppend`
`JVM` specific.
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple classpaths are space separated.

### Description
Append a list of classpaths to the existing classpaths.

## `JVMClasspathPrepend`
`JVM` specific.
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple classpaths are space separated.

### Description
Prepend a list of classpaths to the existing classpaths.

## `JVMClasspath`
A backward-compatibility alias of `JVMClasspathAppend`.

## `ProgramArg`
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple arguments are space separated.

### Description
Specify arguments to a program, as opposed to the runtime.

## `ModifierSet` (preview ⚠️)
### Keys
`val`: `|` separated values, with possible value options. See [here](./index.md#value-options) for details.

### Description
Specify a set of modifiers, including other `ModifierSet`s.
That is, you can use `ModifierSet` recursively.

## `Wrapper` (preview ⚠️)
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple arguments are space separated.

### Description
Specify a wrapper.
If a wrapper also exist for the benchmark suite you use, this wrapper will follow that.

## `Companion` (preview ⚠️)
### Keys
`val`: a single string with [shell-like syntax](https://docs.python.org/3/library/shlex.html#shlex.split).
Multiple arguments are space separated.

### Description
Specify a companion program.
If a companion program also exist for the benchmark suite you use, this companion program will follow that.
