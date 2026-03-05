#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- User-configurable paths ------------------------------------------------
# RUNNING_BENCH_DIR: root of the OCaml benchmark tree (used inside the YAML
#   config via ${RUNNING_BENCH_DIR}).  Defaults to ../benches relative to this
#   script; override with an environment variable if your benchmarks live
#   elsewhere.
export RUNNING_BENCH_DIR="${RUNNING_BENCH_DIR:-$(cd "$ROOT_DIR/../benches" && pwd)}"

LOG_DIR="${LOG_DIR:-$ROOT_DIR/gc-sweep-logs}"
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/src/running/config/ocaml_gc_sweep_example.yml}"
PYTHONPATH="$ROOT_DIR/src"

mkdir -p "$LOG_DIR"

echo "Running GC sweep with config: $CONFIG_FILE"
echo "Benchmark directory: $RUNNING_BENCH_DIR"
echo "Logs root: $LOG_DIR"
PYTHONPATH="$PYTHONPATH" python3 -m running runbms "$LOG_DIR" "$CONFIG_FILE" "$@"
