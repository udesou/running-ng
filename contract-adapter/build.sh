#!/bin/sh
# Build the legacy → contract adapter.
#
# Requires the shared benchmarking data-contract package `bench-contract` in the
# active opam switch. Pin it to the contract repo once:
#     opam pin add bench-contract <path-to-ocaml-bench-dashboard>
#
# Produces contract-adapter/bin/adapter, which running-ng invokes on a legacy run
# directory to emit contract artifacts (see README.md).
set -e
cd "$(dirname "$0")"
dune build ./adapter.exe
mkdir -p bin
cp -f _build/default/adapter.exe bin/adapter
echo "built contract-adapter/bin/adapter (contract schema $(./bin/adapter --schema-version))"
