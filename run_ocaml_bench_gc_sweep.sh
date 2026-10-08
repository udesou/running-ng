#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Provision the tools switch, then run `running runbms` on CONFIG_FILE (which builds olly
# per runtime; OLLY_COMMIT, OLLY_DIR or OLLY_BIN choose which, see running/olly).
# Usage: CONFIG_FILE=<config> LOG_DIR=<dir> [PYTHON=<venv python>] bash run_ocaml_bench_gc_sweep.sh
# RUNNING_BENCH_DIR (micro) and RUNNING_MACRO_BENCH_DIR (macro) are synonyms; default ../benches.
export RUNNING_BENCH_DIR="${RUNNING_BENCH_DIR:-${RUNNING_MACRO_BENCH_DIR:-$(cd "$ROOT_DIR/../benches" 2>/dev/null && pwd || echo "$ROOT_DIR/../benches")}}"
export RUNNING_MACRO_BENCH_DIR="${RUNNING_MACRO_BENCH_DIR:-$RUNNING_BENCH_DIR}"

LOG_DIR="${LOG_DIR:-$ROOT_DIR/gc-sweep-logs}"
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/src/running/config/examples/baseline_micro.yml}"
PYTHONPATH="$ROOT_DIR/src"
# Not auto-detected (.venv, $VIRTUAL_ENV): an explicit variable plus the error below is less surprising.
PYTHON="${PYTHON:-python3}"

# Fail before a switch is provisioned.
if ! PYTHONPATH="$PYTHONPATH" "$PYTHON" -c "import yaml, running" >/dev/null 2>&1; then
  echo "ERROR: '$PYTHON' cannot import running-ng and its dependencies." >&2
  echo "  running-ng is often installed in a virtualenv, whose interpreter" >&2
  echo "  this is not. Either activate it, or point PYTHON at it:" >&2
  echo "    PYTHON=/path/to/venv/bin/python $0" >&2
  exit 1
fi

# Prefer opam 2.3+ (the opam root may require it).
_OPAM=$(command -v opam 2>/dev/null || ([[ -x /usr/local/bin/opam ]] && echo /usr/local/bin/opam))

# Never discovered by scanning `opam switch list`: a switch we did not build has unknown contents.
TOOLS_SWITCH="${TOOLS_SWITCH:-running-ng-tools}"

# Reports ok/adopt/create/rebuild for the tools switch. Stdlib only, so it runs
# before running-ng's dependencies are importable.
if ! PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running.switches ensure; then
  echo "ERROR: could not provision running-ng's opam switches." >&2
  echo "  See: PYTHONPATH=$PYTHONPATH $PYTHON -m running.switches status" >&2
  exit 1
fi

# running-ng's own switches live in its own opam root, never the user's.
TOOLS_OPAMROOT="$(PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running.switches root)"
TOOLS_BIN="$(OPAMROOT="$TOOLS_OPAMROOT" "$_OPAM" var prefix --switch="$TOOLS_SWITCH" 2>/dev/null)/bin"
echo "Tools switch: $TOOLS_SWITCH ($TOOLS_BIN)"
export PATH="$TOOLS_BIN:$PATH"

if [[ ! -x "$TOOLS_BIN/opam-compiler" ]]; then
  echo "ERROR: no opam-compiler at $TOOLS_BIN/opam-compiler." >&2
  echo "  Without it no runtime's opam root can be built: runtime.py runs it" >&2
  echo "  directly to create each compiler switch." >&2
  echo "  Run install_deps_<os>.sh, which provides it in the tools switch." >&2
  exit 1
fi

# opam plugins shell out to `opam` by name; an older opam first on PATH refuses:
#   "Refusing write access to /home/$USER/.opam, which is more recent ..."
_OPAM_DIR="$(dirname "$_OPAM")"
case ":$PATH:" in
  :"$_OPAM_DIR":*) ;;
  *) export PATH="$_OPAM_DIR:$PATH" ;;
esac

mkdir -p "$LOG_DIR"

echo "Running GC sweep with config: $CONFIG_FILE"
echo "Benchmark directory: $RUNNING_BENCH_DIR"
echo "Logs root: $LOG_DIR"
PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running runbms "$LOG_DIR" "$CONFIG_FILE" "$@"
