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
CONFIG_FILE="${CONFIG_FILE:-$ROOT_DIR/src/running/config/examples/ocaml_gc_sweep_example.yml}"
PYTHONPATH="$ROOT_DIR/src"
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
_OPAM=$([[ -x /usr/local/bin/opam ]] && echo /usr/local/bin/opam || command -v opam)

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
  # Ensure dune + ocamlfind are installed.
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

  # Build olly using the tools switch environment.
  eval "$("$_OPAM" env --switch="$TOOLS_SWITCH" --set-switch)"
  (cd "$OLLY_DIR" && dune build @install)

  if [[ ! -x "$OLLY_BIN/olly" ]]; then
    echo "ERROR: dune build succeeded but olly binary not found at $OLLY_BIN/olly" >&2
    exit 1
  fi
  echo "olly built successfully."
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
PYTHONPATH="$PYTHONPATH" python3 -m running runbms "$LOG_DIR" "$CONFIG_FILE" "$@"
