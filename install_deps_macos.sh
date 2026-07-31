#!/usr/bin/env bash
# install_deps_macos.sh — Install all dependencies needed to run
# ~/running-ng/run_ocaml_bench_gc_sweep.sh on a clean macOS machine.
#
# Usage:
#   bash ~/running-ng/install_deps_macos.sh
#
# After this completes successfully, run the benchmark sweep with:
#   ~/running-ng/run_ocaml_bench_gc_sweep.sh
#
# Prerequisites:
#   - macOS with Homebrew (https://brew.sh)
#   - Internet access (for brew, git clones, and opam packages)
#
# What this script does:
#   1. Installs Xcode Command Line Tools if needed
#   2. Installs system packages via Homebrew (autoconf, gmp, python3, etc.)
#   3. Installs opam (OCaml package manager) >= 2.2 if not present
#   4. Creates an opam switch with OCaml 5.4.0
#   5. Installs OCaml tools and libraries needed by various benchmark suites:
#      - dune, ocamlfind (build tools used by most benchmarks)
#      - domainslib (multicore benchmarks)
#      - zarith, lwt, decompress, yojson, etc. (with_packages benchmarks)
#      - hdr_histogram, trace, trace-fuchsia, cmdliner (for olly)
#   6. Builds runtime_events_tools (olly) from source
#   7. Installs Python dependencies (pyyaml)
#   8. Clones the benches repo if not present
#
# Note: perf is Linux-only and not available on macOS.  Configs using
# PerfAndOllyAttach modifiers (perf_grp1/2/3) will not work.  Use the
# olly_gc or time_stats modifiers instead.
#
# The OCaml/OxCaml runtimes used for actual benchmarking are built
# automatically by running-ng on first run — this script only prepares
# the host environment and tools.

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: This script is for macOS only. Use install_deps_linux.sh on Linux." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHES_DIR="${BENCHES_DIR:-$(cd "$ROOT_DIR/.." && pwd)/benches}"
OLLY_DIR="${OLLY_DIR:-$HOME/runtime_events_tools}"
OPAM_SWITCH="5.4.0"

# Minimum opam version required (the ~/.opam directory format requires >= 2.2).
OPAM_MIN_VERSION="2.2.0"

# --- Colors for output -------------------------------------------------------
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }

step() { blue "==> $*"; }
ok()   { green "    OK: $*"; }

# --- Helper: version comparison -----------------------------------------------
# Returns 0 (true) if $1 >= $2.
# Uses GNU sort -V if available (gsort from coreutils), falls back to sort.
_sort_V() {
    if command -v gsort &>/dev/null; then
        gsort -V
    else
        sort -V
    fi
}
version_ge() {
    printf '%s\n%s\n' "$2" "$1" | _sort_V | head -1 | grep -qx "$2"
}

# --- Helper: CPU count -------------------------------------------------------
ncpu() {
    sysctl -n hw.ncpu 2>/dev/null || echo 4
}

# =============================================================================
# 1. Xcode Command Line Tools
# =============================================================================
step "Checking Xcode Command Line Tools"

if xcode-select -p &>/dev/null; then
    ok "Xcode CLT installed at $(xcode-select -p)"
else
    echo "  Installing Xcode Command Line Tools..."
    echo "  (A system dialog may appear — click 'Install' and wait.)"
    xcode-select --install 2>/dev/null || true
    # Wait for installation to complete.
    until xcode-select -p &>/dev/null; do
        sleep 5
    done
    ok "Xcode CLT installed"
fi

# =============================================================================
# 2. Homebrew
# =============================================================================
step "Checking Homebrew"

if ! command -v brew &>/dev/null; then
    echo "  Homebrew not found — installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for this session (Apple Silicon vs Intel).
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

if ! command -v brew &>/dev/null; then
    red "ERROR: Homebrew installation failed or not on PATH"
    exit 1
fi

ok "Homebrew ready"

# =============================================================================
# 3. System packages (via Homebrew)
# =============================================================================
step "Installing system packages via Homebrew"

BREW_PKGS=(
    autoconf                # needed by OxCaml's configure.ac
    git
    curl
    python3
    gmp                     # zarith, pidigits5 (equivalent of libgmp-dev)
    pkg-config              # used by dune to find C libraries
    coreutils               # provides gsort with -V flag, gdate, etc.
    rsync
    unzip
)

# Filter out already-installed packages.
INSTALLED_BREW=$(brew list --formula 2>/dev/null)
TO_INSTALL=()
for pkg in "${BREW_PKGS[@]}"; do
    if ! echo "$INSTALLED_BREW" | grep -qx "$pkg"; then
        TO_INSTALL+=("$pkg")
    fi
done

if [[ ${#TO_INSTALL[@]} -gt 0 ]]; then
    echo "  Installing: ${TO_INSTALL[*]}"
    brew install "${TO_INSTALL[@]}"
else
    ok "All Homebrew packages already installed"
fi

warn "perf is not available on macOS."
warn "PerfAndOllyAttach modifiers (perf_grp1/2/3) will not work."
warn "Use olly_gc or time_stats modifiers instead in your config."

# =============================================================================
# 4. opam (>= 2.2)
# =============================================================================
step "Checking opam"

# Find the best (newest) opam binary on the system.
find_best_opam() {
    local best="" best_ver="0.0.0"
    for candidate in $(which -a opam 2>/dev/null) /opt/homebrew/bin/opam /usr/local/bin/opam; do
        [[ -x "$candidate" ]] || continue
        local ver
        ver=$("$candidate" --version 2>/dev/null) || continue
        if version_ge "$ver" "$best_ver"; then
            best="$candidate"
            best_ver="$ver"
        fi
    done
    echo "$best"
}

OPAM_BIN=$(find_best_opam)
OPAM_VER=""
if [[ -n "$OPAM_BIN" ]]; then
    OPAM_VER=$("$OPAM_BIN" --version)
fi

# Install or upgrade opam if needed.
if [[ -z "$OPAM_BIN" ]] || ! version_ge "$OPAM_VER" "$OPAM_MIN_VERSION"; then
    if [[ -z "$OPAM_BIN" ]]; then
        echo "  opam not found — installing via Homebrew"
    else
        echo "  opam $OPAM_VER found but >= $OPAM_MIN_VERSION required — upgrading"
    fi
    brew install opam || brew upgrade opam
    OPAM_BIN=$(find_best_opam)
    if [[ -z "$OPAM_BIN" ]]; then
        red "ERROR: opam installation failed"
        exit 1
    fi
    OPAM_VER=$("$OPAM_BIN" --version)
    if ! version_ge "$OPAM_VER" "$OPAM_MIN_VERSION"; then
        red "ERROR: opam $OPAM_VER still below required $OPAM_MIN_VERSION"
        exit 1
    fi
fi

echo "  Using opam: $OPAM_BIN (version $OPAM_VER)"

# Initialise opam if needed (no sandboxing on macOS — bubblewrap is Linux-only).
if [[ ! -d "$HOME/.opam" ]]; then
    echo "  Initialising opam (this may take a minute)..."
    "$OPAM_BIN" init --yes --disable-sandboxing --bare
fi

ok "opam ready"

# =============================================================================
# 5. opam switch with OCaml 5.4.0
# =============================================================================
# This switch is used to:
#   - Build olly (runtime_events_tools)
#   - Provide dune, ocamlfind, and opam packages needed by benchmark build
#     scripts (with_packages, with_deps, multicore suites)
#
# Note: The actual benchmark *runtimes* (OCaml/OxCaml compilers used to run
# benchmarks) are built separately by running-ng from source.  This switch
# provides the *build tools* and libraries the benchmark build scripts need.

step "Ensuring opam switch $OPAM_SWITCH"

if ! "$OPAM_BIN" switch list --short 2>/dev/null | grep -qx "$OPAM_SWITCH"; then
    echo "  Creating switch $OPAM_SWITCH (this will take a few minutes)..."
    "$OPAM_BIN" switch create "$OPAM_SWITCH" "ocaml-base-compiler.$OPAM_SWITCH" --yes
else
    ok "Switch $OPAM_SWITCH already exists"
fi

eval "$("$OPAM_BIN" env --switch="$OPAM_SWITCH" --set-switch)"

# =============================================================================
# 6. opam packages
# =============================================================================
step "Installing OCaml packages in switch $OPAM_SWITCH"

# Essential build tools (many benchmark build scripts expect these on PATH).
BUILD_TOOLS=(
    dune                    # build system used by most benchmarks
    ocamlfind               # multicore benchmarks use ocamlfind -package
    opam-compiler           # `opam compiler create` provisions every runtime
                            # switch (runtime.py); without it a run dies with
                            # `unknown command 'compiler'`
)

# Packages needed to build olly (runtime_events_tools).
OLLY_PKGS=(
    cmdliner                # CLI framework
    hdr_histogram           # GC stats histograms
    trace                   # tracing library
    trace-fuchsia           # fuchsia trace format
)

# Benchmark-specific opam packages.
# The build scripts in ~/benches auto-install their own opam deps at build time
# (they create per-compiler opam switches if needed), but pre-installing them
# here into the 5.4.0 switch avoids redundant work and speeds up first runs.
BENCH_PKGS=(
    # multicore/ benchmarks (domainslib)
    domainslib
    # with_packages/ benchmarks
    zarith num              # zarith, benchmarksgame (pidigits5, binarytrees5)
    lwt                     # chameneos, thread-lwt
    decompress              # test_decompress
    bigstringaf checkseum   # decompress deps
    yojson camlp-streams    # ydump
    str                     # benchmarksgame (fasta, spectralnorm)
)

ALL_PKGS=("${BUILD_TOOLS[@]}" "${OLLY_PKGS[@]}" "${BENCH_PKGS[@]}")

echo "  Installing: ${ALL_PKGS[*]}"
"$OPAM_BIN" install --switch="$OPAM_SWITCH" --yes "${ALL_PKGS[@]}"

ok "OCaml packages installed"

# =============================================================================
# 7. Build runtime_events_tools (olly)
# =============================================================================
step "Building runtime_events_tools (olly)"

if [[ ! -d "$OLLY_DIR" ]]; then
    echo "  Cloning runtime_events_tools..."
    git clone https://github.com/tarides/runtime_events_tools.git "$OLLY_DIR"
fi

pushd "$OLLY_DIR" >/dev/null

eval "$("$OPAM_BIN" env --switch="$OPAM_SWITCH" --set-switch)"
dune build -p runtime_events_tools -j "$(ncpu)" @install 2>&1 | tail -5

OLLY_EXE="$OLLY_DIR/_build/install/default/bin/olly"
if [[ -x "$OLLY_EXE" ]]; then
    ok "olly built at $OLLY_EXE"
else
    red "ERROR: olly binary not found after build"
    echo "  Expected at: $OLLY_EXE"
    echo "  Check build output above for errors."
    popd >/dev/null
    exit 1
fi

popd >/dev/null

# =============================================================================
# 8. Python dependencies
# =============================================================================
step "Installing Python dependencies"

pip3 install --user --quiet pyyaml 2>/dev/null \
    || pip3 install --quiet --break-system-packages pyyaml 2>/dev/null \
    || pip3 install --quiet pyyaml
ok "pyyaml installed"

# =============================================================================
# 9. Benchmarks repo
# =============================================================================
step "Checking benchmarks directory"

if [[ -d "$BENCHES_DIR" ]]; then
    ok "Benchmarks found at $BENCHES_DIR"
else
    echo "  Cloning benches repo to $BENCHES_DIR..."
    git clone https://github.com/udesou/benches.git "$BENCHES_DIR"
    ok "Benchmarks cloned to $BENCHES_DIR"
fi

# =============================================================================
# 10. Verify installation
# =============================================================================
step "Verifying installation"

ERRORS=0

check_cmd() {
    if command -v "$1" &>/dev/null; then
        ok "$1"
    else
        red "MISSING: $1"
        ERRORS=$((ERRORS + 1))
    fi
}

check_file() {
    if [[ -e "$1" ]]; then
        ok "$1"
    else
        red "MISSING: $1"
        ERRORS=$((ERRORS + 1))
    fi
}

# Activate the switch for verification.
eval "$("$OPAM_BIN" env --switch="$OPAM_SWITCH" --set-switch)"

echo "  System commands:"
check_cmd python3
check_cmd git
check_cmd autoconf
check_cmd make
check_cmd cc
check_cmd "$OPAM_BIN"
check_cmd brew

echo "  OCaml tools (from switch $OPAM_SWITCH):"
check_cmd dune
check_cmd ocamlfind
check_cmd ocamlopt

echo "  Files:"
check_file "$OLLY_EXE"
check_file "$BENCHES_DIR"
check_file "$ROOT_DIR/src/running/config/ocaml_gc_sweep_example.yml"

echo "  Python modules:"
python3 -c "import yaml" 2>/dev/null && ok "pyyaml" || {
    red "MISSING: pyyaml"
    ERRORS=$((ERRORS + 1))
}

echo "  OCaml packages:"
for pkg in dune ocamlfind domainslib zarith lwt decompress yojson; do
    if "$OPAM_BIN" list --installed --short "$pkg" --switch="$OPAM_SWITCH" 2>/dev/null | grep -qx "$pkg"; then
        ok "$pkg"
    else
        red "MISSING: $pkg"
        ERRORS=$((ERRORS + 1))
    fi
done

echo ""
if [[ $ERRORS -eq 0 ]]; then
    green "All dependencies installed successfully!"
    echo ""
    echo "To run the benchmark sweep:"
    echo "  ~/running-ng/run_ocaml_bench_gc_sweep.sh"
    echo ""
    echo "Notes:"
    echo "  - perf is not available on macOS. Make sure your config uses"
    echo "    olly_gc or time_stats modifiers instead of perf_grp1/2/3."
    echo "  - The first run will take longer as it builds OCaml/OxCaml runtimes."
    echo "    Subsequent runs reuse cached toolchains in /tmp/running-ng-ocaml-toolchains/."
    echo "  - Edit the config file to enable/disable benchmark suites:"
    echo "    $ROOT_DIR/src/running/config/ocaml_gc_sweep_example.yml"
    echo "  - The opam switch '$OPAM_SWITCH' should be active when running benchmarks"
    echo "    that need dune/ocamlfind (with_packages, with_deps, multicore suites)."
    echo "    Run: eval \$($OPAM_BIN env --switch=$OPAM_SWITCH --set-switch)"
else
    red "$ERRORS dependency check(s) failed — see above for details."
    exit 1
fi
