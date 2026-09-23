#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Provision the tools/olly switches, build olly, then run `running runbms` on CONFIG_FILE.
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

# Exported: running.switches keys the olly switch on this checkout's SHA and resolves
# OLLY_DIR itself; unexported, the two could pick different checkouts and rebuild every run.
export OLLY_DIR="${OLLY_DIR:-$(cd "$ROOT_DIR/../runtime_events_tools" 2>/dev/null && pwd || echo "$HOME/runtime_events_tools")}"
# An explicit OLLY_BIN means "use this olly": the build below leaves it alone.
_OLLY_BIN_EXPLICIT=0
if [[ -n "${OLLY_BIN:-}" ]]; then
  _OLLY_BIN_EXPLICIT=1
fi
OLLY_BIN="${OLLY_BIN:-$OLLY_DIR/_build/install/default/bin}"

# `olly gc-stats --json` must emit max_rss_kb (tarides/runtime_events_tools#85); the
# feature commit is checked, not the merge, so any branch based on it passes.
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
    echo "  The build below then picks the new revision up on its own." >&2
    exit 1
  fi
fi


# Prefer opam 2.3+ (the opam root may require it).
_OPAM=$(command -v opam 2>/dev/null || ([[ -x /usr/local/bin/opam ]] && echo /usr/local/bin/opam))

# Never discovered by scanning `opam switch list`: a switch we did not build has unknown contents.
TOOLS_SWITCH="${TOOLS_SWITCH:-running-ng-tools}"
OLLY_SWITCH="${OLLY_SWITCH:-running-ng-olly}"

# Reports ok/adopt/create/rebuild per switch (the olly switch is keyed on the checkout
# SHA). Stdlib only, so it runs before running-ng's dependencies are importable.
if ! PYTHONPATH="$PYTHONPATH" "$PYTHON" -m running.switches ensure; then
  echo "ERROR: could not provision running-ng's opam switches." >&2
  echo "  See: PYTHONPATH=$PYTHONPATH $PYTHON -m running.switches status" >&2
  exit 1
fi

TOOLS_BIN="$("$_OPAM" var prefix --switch="$TOOLS_SWITCH" 2>/dev/null)/bin"
echo "Tools switch: $TOOLS_SWITCH ($TOOLS_BIN)"
export PATH="$TOOLS_BIN:$PATH"

# Provisioning belongs to install_deps_<os>.sh. Installing here corrupted switches:
# opam-compiler pins cmdliner < 2.0 (breaks olly: "Unbound module Arg.Conv") and olly's
# deps pull cmdliner >= 2.0 (evicts opam-compiler), so the two need separate switches.
_OPAM_PLUGIN_BIN="$("$_OPAM" var root 2>/dev/null)/plugins/bin/opam-compiler"
# -x is false for a dangling symlink, which a rebuilt tools switch leaves behind.
if [[ ! -x "$_OPAM_PLUGIN_BIN" ]]; then
  echo "ERROR: the opam 'compiler' plugin is not registered at" >&2
  echo "  $_OPAM_PLUGIN_BIN" >&2
  echo "  Without it no runtime switch can be provisioned: runtime.py runs" >&2
  echo "  'opam compiler create', which resolves opam-compiler as a plugin," >&2
  echo "  and fails with \"unknown command 'compiler'\"." >&2
  echo "  Run install_deps_<os>.sh, which registers it against the tools switch." >&2
  exit 1
fi

# Unconditional: dune is incremental (sub-second no-op), and a presence guard would
# silently skip a stale _build after a `git pull` in $OLLY_DIR, measuring with an olly
# from a different revision than the run records. The bench agent also deletes _build
# when the olly pin moves and relies on this rebuild. Built in OLLY_SWITCH, never
# TOOLS_SWITCH (see above); running.switches ensure has already resolved the deps.
if [[ "$_OLLY_BIN_EXPLICIT" == 1 && -x "$OLLY_BIN/olly" ]]; then
  echo "Using olly from OLLY_BIN: $OLLY_BIN"
elif [[ ! -d "$OLLY_DIR" ]]; then
  echo "ERROR: no runtime_events_tools checkout at $OLLY_DIR." >&2
  echo "  Run install_deps_<os>.sh, which clones it, or set OLLY_DIR to a" >&2
  echo "  checkout, or OLLY_BIN to an existing build." >&2
  exit 1
else
  echo "Building olly from $OLLY_DIR in switch $OLLY_SWITCH (incremental) ..."
  # Log kept off the console (a published artifact); not piped to tail, which would mask dune's exit status.
  _OLLY_BUILD_LOG="${TMPDIR:-/tmp}/running-ng-olly-build.log"
  if ! (
    cd "$OLLY_DIR"
    # No --set-switch: it would leave the user's global switch pointing at ours on an
    # early exit. Captured, not eval'd inline, so a failure is not swallowed.
    _olly_env="$("$_OPAM" env --switch="$OLLY_SWITCH")" || exit 1
    eval "$_olly_env"
    # No -j: dune defaults to the core count and nproc does not exist on FreeBSD/macOS.
    dune build -p runtime_events_tools @install
  ) > "$_OLLY_BUILD_LOG" 2>&1; then
    echo "ERROR: the olly build failed. Last 30 lines of $_OLLY_BUILD_LOG:" >&2
    tail -30 "$_OLLY_BUILD_LOG" >&2
    exit 1
  fi
fi

# Post-condition: the build succeeded, so a missing binary means the package stopped installing olly or OLLY_BIN is wrong.
if [[ ! -x "$OLLY_BIN/olly" ]]; then
  echo "ERROR: olly is not at $OLLY_BIN/olly after a successful build." >&2
  echo "  If OLLY_BIN is set, it must point at the install directory of a" >&2
  echo "  runtime_events_tools build (.../_build/install/default/bin)." >&2
  exit 1
fi

export PATH="$OLLY_BIN:$PATH"

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
