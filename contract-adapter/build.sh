#!/bin/sh
# Build the legacy → contract adapter.
#
# Requires, in the active opam switch: the shared data-contract package
# `bench-contract` (pin it once to the contract repo) and `yaml`:
#     opam pin add bench-contract <path-to-ocaml-bench-dashboard>
#     opam install yaml
#
# Produces contract-adapter/bin/adapter, which running-ng invokes on a legacy run
# directory to emit contract artifacts (see README.md).
set -e
cd "$(dirname "$0")"
dune build ./adapter.exe
mkdir -p bin
cp -f _build/default/adapter.exe bin/adapter
echo "built contract-adapter/bin/adapter (contract schema $(./bin/adapter --schema-version))"
