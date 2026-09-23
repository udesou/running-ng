#!/bin/sh
# Build contract-adapter/bin/adapter (legacy run dir -> contract artifacts). Needs
# `bench-contract` (opam pin add bench-contract <ocaml-bench-dashboard>) and `yaml` in the active switch.
set -e
cd "$(dirname "$0")"
dune build ./adapter.exe
mkdir -p bin
cp -f _build/default/adapter.exe bin/adapter
echo "built contract-adapter/bin/adapter (contract schema $(./bin/adapter --schema-version))"
