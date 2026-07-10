# contract-adapter

Producer-side shim that converts a **legacy** running-ng run directory (filename
metadata + raw `olly_*`/`perf_*` NDJSON sidecars) into **data-contract** artifacts
that the ingestor and dashboard consume:

```
<out-dir>/manifest.json               # run manifest (configs, machine, comparisons, _produced_by)
<out-dir>/measurements/olly.ndjson    # olly's per-invocation measurement records (NDJSON)
<out-dir>/measurements/perf.ndjson    # perf's records; the ingestor merges the two by identity
```
(Per-tool NDJSON — olly and perf write separately and the ingestor merges records
sharing `(run_id, config_id, benchmark, invocation)`.)

This is the **only** component that knows the legacy on-disk layout. It exists so
the current (unversioned) runner interoperates with the contract without changing
running-ng's internals:

```
running-ng (unversioned) → legacy output → [contract-adapter] → contract → ingestor
running-ng (versioned)   → contract output ─────────────────────────────→ ingestor   (future; adapter bypassed)
```

## Build

The adapter depends on the shared contract package **`bench-contract`** (OCaml
module `Schema`), which lives in the dashboard/contract repo. Pin it once, then
build:

```sh
opam pin add bench-contract <path-to-ocaml-bench-dashboard>   # once
./build.sh                                                     # -> contract-adapter/bin/adapter
```

## Use

Directly:
```sh
bin/adapter <legacy-run-dir> <out-dir>   # legacy run dir -> contract artifacts
bin/adapter --schema-version             # contract version this adapter was built against
```

Or via running-ng (recommended — adds the schema_version switch + version check):
```sh
python3 -m running adapt <legacy-run-dir>            # -> <run>/contract/
python3 -m running adapt <run> -c <config.yml>       # skips the adapter if the config sets schema_version
```
`running adapt` locates this binary (`--adapter`, `$RUNNING_CONTRACT_ADAPTER`, or
`contract-adapter/bin/adapter`) and warns if it was built against an older schema
than the installed `bench-contract` package.

## Schema-version awareness

`bin/adapter --schema-version` prints the `bench-contract` schema version the
binary was compiled against. running-ng compares this against the
`bench-contract` package installed in the switch; if the package is newer, the
adapter is out of date and should be rebuilt (`./build.sh`) so its output matches
the current contract. This is the versioned link between running-ng and the
contract: bump `bench-contract`, rebuild the adapter, and any drift is flagged
rather than silently emitting stale-shaped data.

Once running-ng emits contract artifacts natively (config `schema_version` set),
this adapter is used only for legacy / archived runs.
