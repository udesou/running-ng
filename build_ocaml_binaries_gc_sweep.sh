#!/usr/bin/env bash
# build_ocaml_binaries_gc_sweep.sh — Build all benchmark binaries without running them.
#
# This script mirrors run_ocaml_bench_gc_sweep.sh but only compiles the
# benchmark binaries.  Useful for verifying that all benchmarks build
# successfully with the configured runtimes before committing to a full sweep.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- User-configurable paths ------------------------------------------------
export RUNNING_BENCH_DIR="${RUNNING_BENCH_DIR:-$(cd "$ROOT_DIR/../benches" && pwd)}"
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/src/running/config/examples/ocaml_gc_sweep_example.yml}"
PYTHONPATH="$ROOT_DIR/src"
OLLY_DIR="${OLLY_DIR:-$(cd "$ROOT_DIR/../runtime_events_tools" 2>/dev/null && pwd || echo "$HOME/runtime_events_tools")}"
OLLY_BIN="${OLLY_BIN:-$OLLY_DIR/_build/install/default/bin}"

# --- Ensure a tools switch with dune/ocamlfind exists ----------------------
_OPAM=$(command -v opam 2>/dev/null || ([[ -x /usr/local/bin/opam ]] && echo /usr/local/bin/opam))

TOOLS_SWITCH="${TOOLS_SWITCH:-}"
if [[ -z "$TOOLS_SWITCH" ]]; then
  for _sw in $("$_OPAM" switch list --short 2>/dev/null); do
    [[ "$_sw" == running-ng-oxcaml-build ]] && continue
    [[ "$_sw" == ext-* ]] && continue
    if [[ -x "$("$_OPAM" var prefix --switch="$_sw" 2>/dev/null)/bin/dune" ]]; then
      TOOLS_SWITCH="$_sw"
      break
    fi
  done
fi

if [[ -z "$TOOLS_SWITCH" ]]; then
  TOOLS_SWITCH="running-ng-tools"
  if "$_OPAM" switch list --short 2>/dev/null | grep -qFx "$TOOLS_SWITCH"; then
    echo "Reusing existing tools switch '$TOOLS_SWITCH'."
  else
    echo "No opam switch with dune found. Creating '$TOOLS_SWITCH'..."
    _OCAML_VER=$("$_OPAM" show ocaml-base-compiler --field=all-versions 2>/dev/null \
      | tr ' ' '\n' | grep -E '^5\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
    : "${_OCAML_VER:=5.3.0}"
    echo "Using OCaml ${_OCAML_VER} (compiles from source — may take a few minutes)..."
    "$_OPAM" switch create "$TOOLS_SWITCH" "ocaml-base-compiler.${_OCAML_VER}" --yes
  fi
  "$_OPAM" install --switch "$TOOLS_SWITCH" --yes dune ocamlfind
fi

TOOLS_BIN="$("$_OPAM" var prefix --switch="$TOOLS_SWITCH" 2>/dev/null)/bin"
echo "Tools switch: $TOOLS_SWITCH ($TOOLS_BIN)"
export PATH="$TOOLS_BIN:$PATH"

# --- Build olly if it hasn't been built yet --------------------------------
if [[ ! -x "$OLLY_BIN/olly" ]]; then
  echo "olly not found at $OLLY_BIN/olly — building from $OLLY_DIR ..."
  if [[ ! -d "$OLLY_DIR" ]]; then
    echo "ERROR: runtime_events_tools directory not found at $OLLY_DIR" >&2
    echo "  Set OLLY_DIR or OLLY_BIN to point to your runtime_events_tools checkout." >&2
    exit 1
  fi
  echo "Installing runtime_events_tools opam dependencies..."
  (cd "$OLLY_DIR" && "$_OPAM" install . --deps-only --switch "$TOOLS_SWITCH" --yes)
  eval "$("$_OPAM" env --switch="$TOOLS_SWITCH" --set-switch)"
  (cd "$OLLY_DIR" && dune build @install)
  if [[ ! -x "$OLLY_BIN/olly" ]]; then
    echo "ERROR: dune build succeeded but olly binary not found at $OLLY_BIN/olly" >&2
    exit 1
  fi
  echo "olly built successfully."
fi

export PATH="$OLLY_BIN:$PATH"

echo "Building benchmark binaries with config: $CONFIG_FILE"
echo "Benchmark directory: $RUNNING_BENCH_DIR"
PYTHONPATH="$PYTHONPATH" python3 -m running buildbms "$CONFIG_FILE" "$@"
