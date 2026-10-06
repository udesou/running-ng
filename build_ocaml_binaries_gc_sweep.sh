#!/usr/bin/env bash
# Build all benchmark binaries without running them (the build half of
# run_ocaml_bench_gc_sweep.sh). Usage: CONFIG_FILE=<config> bash build_ocaml_binaries_gc_sweep.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# RUNNING_BENCH_DIR (micro configs) and RUNNING_MACRO_BENCH_DIR (macro configs) are
# synonyms; both must be exported so a config of either kind resolves its paths.
export RUNNING_BENCH_DIR="${RUNNING_BENCH_DIR:-${RUNNING_MACRO_BENCH_DIR:-$(cd "$ROOT_DIR/../benches" 2>/dev/null && pwd || echo "$ROOT_DIR/../benches")}}"
export RUNNING_MACRO_BENCH_DIR="${RUNNING_MACRO_BENCH_DIR:-$RUNNING_BENCH_DIR}"
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/src/running/config/examples/baseline_micro.yml}"
PYTHONPATH="$ROOT_DIR/src"

_OPAM=$(command -v opam 2>/dev/null || ([[ -x /usr/local/bin/opam ]] && echo /usr/local/bin/opam))
PYTHON="${PYTHON:-python3}"
TOOLS_SWITCH="${TOOLS_SWITCH:-running-ng-tools}"

# The same provisioning as run_ocaml_bench_gc_sweep.sh; building needs no olly.
if ! PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running.switches ensure --switch "$TOOLS_SWITCH"; then
  echo "ERROR: could not provision running-ng's tools switch." >&2
  echo "  See: PYTHONPATH=$PYTHONPATH $PYTHON -m running.switches status" >&2
  exit 1
fi
TOOLS_OPAMROOT="$(PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running.switches root)"
TOOLS_BIN="$(OPAMROOT="$TOOLS_OPAMROOT" "$_OPAM" var prefix --switch="$TOOLS_SWITCH" 2>/dev/null)/bin"
echo "Tools switch: $TOOLS_SWITCH ($TOOLS_BIN)"
export PATH="$TOOLS_BIN:$PATH"

if [[ ! -x "$TOOLS_BIN/opam-compiler" ]]; then
  echo "ERROR: no opam-compiler at $TOOLS_BIN/opam-compiler." >&2
  echo "  Run install_deps_<os>.sh, which provides it in the tools switch." >&2
  exit 1
fi

echo "Building benchmark binaries with config: $CONFIG_FILE"
echo "Benchmark directory: $RUNNING_BENCH_DIR"
PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running buildbms "$CONFIG_FILE" "$@"
