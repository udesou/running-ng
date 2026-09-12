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
# Whether OLLY_BIN was pinned by the caller rather than derived from OLLY_DIR.
# An explicit OLLY_BIN means "use this olly", so the build below leaves it
# alone; a derived one is ours to (re)build.
_OLLY_BIN_EXPLICIT=0
if [[ -n "${OLLY_BIN:-}" ]]; then
  _OLLY_BIN_EXPLICIT=1
fi
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
    echo "  The build below then picks the new revision up on its own." >&2
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
  echo "  See: PYTHONPATH=$PYTHONPATH $PYTHON -m running.switches status" >&2
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

# --- Build olly ------------------------------------------------------------
# Unconditional, NOT guarded on the binary being absent. dune is already an
# incremental build system, so with nothing to do this is a sub-second no-op,
# and it is the only formulation that covers all three cases. A presence guard
# covers the first two and silently skips the third:
#
#   no _build at all         first run on a machine, or install_deps_<os>.sh
#                            was never run here
#   _build deleted           the bench agent drops it whenever the olly pin
#                            moves, then relies on this script to rebuild
#                            (bench_agent.ml, "a pin move must invalidate the
#                            cached _build")
#   _build present, stale    a plain `git pull` in $OLLY_DIR, or any caller
#                            that moves the checkout without deleting _build
#
# The third case is the dangerous one: it does not fail the run, it silently
# measures with an olly built from a different revision than the one this run
# records. That is worse than a crash, and a presence guard cannot see it.
#
# This is also why the build lives here rather than only in install_deps_<os>.sh:
# nothing else runs between the agent deleting _build and the sweep starting,
# so a sweep that cannot build olly can only fail. That gap is exactly how a
# run died after the on-demand build was removed from this script.
#
# Built in OLLY_SWITCH, never TOOLS_SWITCH: installing olly's deps into the
# tools switch evicts opam-compiler, which is the corruption described above.
# running.switches ensure (above) has already created the switch and resolved
# olly's dependencies into it, so only the build itself is left here.
if [[ "$_OLLY_BIN_EXPLICIT" == 1 && -x "$OLLY_BIN/olly" ]]; then
  # The caller pointed at a specific olly. Ours to use, not ours to rebuild.
  echo "Using olly from OLLY_BIN: $OLLY_BIN"
elif [[ ! -d "$OLLY_DIR" ]]; then
  echo "ERROR: no runtime_events_tools checkout at $OLLY_DIR." >&2
  echo "  Run install_deps_<os>.sh, which clones it, or set OLLY_DIR to a" >&2
  echo "  checkout, or OLLY_BIN to an existing build." >&2
  exit 1
else
  echo "Building olly from $OLLY_DIR in switch $OLLY_SWITCH (incremental) ..."
  # Kept out of the console on success: a from-scratch olly build is hundreds
  # of lines and this console log is a published run artifact. Not piped to
  # `tail`, which would report tail's exit status rather than dune's.
  _OLLY_BUILD_LOG="${TMPDIR:-/tmp}/running-ng-olly-build.log"
  if ! (
    cd "$OLLY_DIR"
    # No --set-switch: this must affect the build and nothing else. The rest
    # of the script needs TOOLS_BIN on PATH, and --set-switch would leave the
    # user's global switch pointing at ours if we exited early.
    # Captured rather than eval'd inline so a failure here is not swallowed
    # by eval, which would silently leave dune resolving to the tools switch.
    _olly_env="$("$_OPAM" env --switch="$OLLY_SWITCH")" || exit 1
    eval "$_olly_env"
    # No -j: dune already defaults to the core count, and `nproc` does not
    # exist on FreeBSD or macOS, where this same script runs.
    dune build -p runtime_events_tools @install
  ) > "$_OLLY_BUILD_LOG" 2>&1; then
    echo "ERROR: the olly build failed. Last 30 lines of $_OLLY_BUILD_LOG:" >&2
    tail -30 "$_OLLY_BUILD_LOG" >&2
    exit 1
  fi
fi

# A post-condition, not a provisioning check: the build above reported success,
# so a missing binary here means the package stopped installing olly, or that
# OLLY_BIN points somewhere other than the build's install directory.
if [[ ! -x "$OLLY_BIN/olly" ]]; then
  echo "ERROR: olly is not at $OLLY_BIN/olly after a successful build." >&2
  echo "  If OLLY_BIN is set, it must point at the install directory of a" >&2
  echo "  runtime_events_tools build (.../_build/install/default/bin)." >&2
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
