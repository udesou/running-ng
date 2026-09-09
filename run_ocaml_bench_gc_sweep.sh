#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- User-configurable paths ------------------------------------------------
# RUNNING_BENCH_DIR / RUNNING_MACRO_BENCH_DIR: root of the OCaml benchmark
# tree.  The two names are synonyms — YAML configs use ${RUNNING_MACRO_BENCH_DIR};
# the script also accepts the shorter RUNNING_BENCH_DIR.  Defaults to
# ../benches relative to this script if neither is set.
export RUNNING_BENCH_DIR="${RUNNING_BENCH_DIR:-${RUNNING_MACRO_BENCH_DIR:-$(cd "$ROOT_DIR/../benches" 2>/dev/null && pwd || echo "$ROOT_DIR/../benches")}}"
export RUNNING_MACRO_BENCH_DIR="${RUNNING_MACRO_BENCH_DIR:-$RUNNING_BENCH_DIR}"

LOG_DIR="${LOG_DIR:-$ROOT_DIR/gc-sweep-logs}"
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/src/running/config/examples/ocaml_gc_sweep_example.yml}"
PYTHONPATH="$ROOT_DIR/src"
# Honour an explicit interpreter. running-ng is commonly installed into a
# virtualenv, in which case the system python3 has none of its dependencies,
# so a hardcoded `python3` cannot run the harness at all. Deliberately not
# auto-detected: guessing at .venv/, $VIRTUAL_ENV or a hardcoded path is more
# surprising than an explicit variable plus the clear error below.
PYTHON="${PYTHON:-python3}"

# --- Verify the interpreter can actually run the harness --------------------
# Cheaper to fail here than after a switch has been provisioned.
if ! PYTHONPATH="$PYTHONPATH" "$PYTHON" -c "import yaml, running" >/dev/null 2>&1; then
  echo "ERROR: '$PYTHON' cannot import running-ng and its dependencies." >&2
  echo "  running-ng is often installed in a virtualenv, whose interpreter" >&2
  echo "  this is not. Either activate it, or point PYTHON at it:" >&2
  echo "    PYTHON=/path/to/venv/bin/python $0" >&2
  exit 1
fi

OLLY_DIR="${OLLY_DIR:-$(cd "$ROOT_DIR/../runtime_events_tools" 2>/dev/null && pwd || echo "$HOME/runtime_events_tools")}"
OLLY_BIN="${OLLY_BIN:-$OLLY_DIR/_build/install/default/bin}"

# --- Verify runtime_events_tools is recent enough --------------------------
# The benchmark pipeline relies on `olly gc-stats --json` emitting `max_rss_kb`
# (tarides/runtime_events_tools#85).  If the local checkout predates that
# change, the .json sidecar will silently lack RSS data — so fail loudly at
# setup time rather than halfway through a long sweep.  We check the feature
# commit (not the merge commit) so the check works from any branch based on
# or after it.
REQUIRED_OLLY_COMMIT="977e33b6dea5e3bbcf13557a31513b11dfbfc4d5"
if [[ -d "$OLLY_DIR/.git" ]]; then
  if ! git -C "$OLLY_DIR" cat-file -e "$REQUIRED_OLLY_COMMIT" 2>/dev/null; then
    echo "Fetching latest refs in $OLLY_DIR ..."
    git -C "$OLLY_DIR" fetch --quiet || true
  fi
  if ! git -C "$OLLY_DIR" merge-base --is-ancestor "$REQUIRED_OLLY_COMMIT" HEAD 2>/dev/null; then
    echo "ERROR: runtime_events_tools at $OLLY_DIR is out of date." >&2
    echo "  HEAD does not contain $REQUIRED_OLLY_COMMIT" >&2
    echo "  (tarides/runtime_events_tools#85, required for max_rss_kb in --json output)." >&2
    echo "  Update: cd '$OLLY_DIR' && git checkout main && git pull" >&2
    echo "  Then delete the stale binary: rm -rf '$OLLY_DIR/_build'" >&2
    exit 1
  fi
fi

# --- Ensure a tools switch with dune/ocamlfind exists ----------------------
# Benchmark build scripts and olly need dune + ocamlfind.  The user's active
# opam switch may be the OxCaml bootstrap switch which lacks these tools.
# We find or create a dedicated "running-ng-tools" switch and put its bin/
# on PATH for the whole run.

# Prefer opam 2.3+ (the opam root may require it).
_OPAM=$(command -v opam 2>/dev/null || ([[ -x /usr/local/bin/opam ]] && echo /usr/local/bin/opam))

# running-ng declares its own switches; running.switches owns creating and
# validating them. Never discovered by scanning: this used to take the first
# switch in `opam switch list` containing dune, which picked a LOCAL switch on
# one machine and the olly switch on another. A switch we did not build has
# unknown contents.
TOOLS_SWITCH="${TOOLS_SWITCH:-running-ng-tools}"
OLLY_SWITCH="${OLLY_SWITCH:-running-ng-olly}"

# Reports 'ok', 'adopt', 'create' or 'rebuild' per switch, and rebuilds one
# whose contents no longer match what it was built from (for the olly switch
# that includes the checkout's git SHA, so moving it rebuilds olly). Restores
# the active switch afterwards. Runs on stdlib only, so it works before
# running-ng's dependencies are importable.
if ! PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running.switches ensure; then
  echo "ERROR: could not provision running-ng's opam switches." >&2
  echo "  See: $PYTHON -m running.switches status" >&2
  exit 1
fi

TOOLS_BIN="$("$_OPAM" var prefix --switch="$TOOLS_SWITCH" 2>/dev/null)/bin"
echo "Tools switch: $TOOLS_SWITCH ($TOOLS_BIN)"
export PATH="$TOOLS_BIN:$PATH"

# --- Verify the environment; provisioning belongs to install_deps ----------
# This script used to install opam-compiler and olly's dependencies itself,
# both into TOOLS_SWITCH. That predates the two-switch split and could corrupt
# whichever switch it picked:
#
#   into the olly switch : opam-compiler pins cmdliner < 2.0, so cmdliner is
#                          DOWNGRADED and olly can no longer be rebuilt
#                          ("Error: Unbound module Arg.Conv")
#   into the tools switch: olly's deps pull cmdliner >= 2.0, which EVICTS
#                          opam-compiler ("conflicts with cmdliner"), so
#                          `opam compiler create` stops working
#
# Both were reachable only by luck, because the "already done" guards skipped
# them on a machine that was already set up. So the script now checks and
# points at install_deps_<os>.sh, which owns provisioning and knows about both
# switches. Duplicating that job here is how the two drifted apart.
_OPAM_PLUGIN_BIN="$("$_OPAM" var root 2>/dev/null)/plugins/bin/opam-compiler"
# `-x` follows symlinks, so it is false for a dangling one too, which is what
# rebuilding the tools switch leaves behind.
if [[ ! -x "$_OPAM_PLUGIN_BIN" ]]; then
  echo "ERROR: the opam 'compiler' plugin is not registered at" >&2
  echo "  $_OPAM_PLUGIN_BIN" >&2
  echo "  Without it no runtime switch can be provisioned: runtime.py runs" >&2
  echo "  'opam compiler create', which resolves opam-compiler as a plugin," >&2
  echo "  and fails with \"unknown command 'compiler'\"." >&2
  echo "  Run install_deps_<os>.sh, which registers it against the tools switch." >&2
  exit 1
fi

if [[ ! -x "$OLLY_BIN/olly" ]]; then
  echo "ERROR: olly not found at $OLLY_BIN/olly." >&2
  echo "  Run install_deps_<os>.sh, which builds it in its own switch" >&2
  echo "  ('$OLLY_SWITCH'), or point OLLY_DIR/OLLY_BIN at an existing build." >&2
  exit 1
fi

export PATH="$OLLY_BIN:$PATH"

# Ensure the chosen opam binary is first on PATH.  opam plugins (notably
# opam-compiler) shell out to `opam` by name, so if PATH resolves to an older
# opam than `$_OPAM` the plugin refuses to write a newer root with:
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
