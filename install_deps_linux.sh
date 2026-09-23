#!/usr/bin/env bash
# Prepare a clean Ubuntu/Debian host to run run_ocaml_bench_gc_sweep.sh: apt packages
# (build tools, perf, python3, libgmp), opam >= 2.2, the running-ng tools and olly
# switches, olly built from source, pyyaml, and a ~/benches clone. Needs sudo.
# Benchmark runtimes are not built here; running-ng provisions them per config.
# Usage: bash install_deps_linux.sh   (or bash install_deps.sh, which dispatches)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHES_DIR="${BENCHES_DIR:-$(cd "$ROOT_DIR/.." && pwd)/benches}"
OLLY_DIR="${OLLY_DIR:-$HOME/runtime_events_tools}"
# Not "5.4.0": this is the name running.switches and the FreeBSD installer declare.
OPAM_SWITCH="${OPAM_SWITCH:-running-ng-tools}"
OCAML_VERSION="${OCAML_VERSION:-5.4.0}"
# olly needs cmdliner >= 2.0; opam-compiler (tools switch) pins it < 2.0.
OLLY_SWITCH="${OLLY_SWITCH:-running-ng-olly}"

# The ~/.opam directory format requires >= 2.2.
OPAM_MIN_VERSION="2.2.0"

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }

step() { blue "==> $*"; }
ok()   { green "    OK: $*"; }

version_ge() {
    printf '%s\n%s\n' "$2" "$1" | sort -V | head -1 | grep -qx "$2"
}

# 1. System packages
step "Installing system packages"

REQUIRED_PKGS=(
    build-essential
    # OxCaml's configure.ac requires autoconf to generate ./configure
    autoconf
    git
    curl
    python3
    python3-pip
    python3-venv
    # perf
    linux-tools-common
    linux-tools-generic
    libgmp-dev              # zarith, pidigits5
    pkg-config
    bubblewrap
    unzip
    rsync
)

# May not exist for this kernel.
LINUX_TOOLS_PKG="linux-tools-$(uname -r)"

TO_INSTALL=()
for pkg in "${REQUIRED_PKGS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        TO_INSTALL+=("$pkg")
    fi
done
if ! dpkg -s "$LINUX_TOOLS_PKG" &>/dev/null; then
    TO_INSTALL+=("$LINUX_TOOLS_PKG")
fi

if [[ ${#TO_INSTALL[@]} -gt 0 ]]; then
    echo "  Installing: ${TO_INSTALL[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${TO_INSTALL[@]}" || {
        warn "Some packages failed to install (e.g. linux-tools for this kernel)."
        warn "perf may not be available — PerfAndOllyAttach modifiers will fail."
        warn "You can install perf manually or switch to olly_gc/time_stats modifiers."
    }
else
    ok "All system packages already installed"
fi

if ! command -v perf &>/dev/null; then
    warn "perf not found. PerfAndOllyAttach modifiers (perf_grp1/2/3) will fail."
    warn "Try: sudo apt install linux-tools-\$(uname -r) linux-tools-generic"
fi

# 2. opam (>= 2.2)
step "Checking opam"

find_best_opam() {
    local best="" best_ver="0.0.0"
    for candidate in $(which -a opam 2>/dev/null) /usr/local/bin/opam /usr/bin/opam; do
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

if [[ -z "$OPAM_BIN" ]] || ! version_ge "$OPAM_VER" "$OPAM_MIN_VERSION"; then
    if [[ -z "$OPAM_BIN" ]]; then
        echo "  opam not found — installing via official script"
    else
        echo "  opam $OPAM_VER found but >= $OPAM_MIN_VERSION required — upgrading"
    fi
    bash -c "sh <(curl -fsSL https://opam.ocaml.org/install.sh)" -- --yes
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

if [[ ! -d "$HOME/.opam" ]]; then
    echo "  Initialising opam (this may take a minute)..."
    "$OPAM_BIN" init --yes --disable-sandboxing --bare
fi

ok "opam ready"

# 3. Switches

step "Provisioning running-ng's opam switches"
# running.switches is the single declaration of the tools switch and the separate
# olly switch (cmdliner >= 2.0 vs opam-compiler's < 2.0 pin).
OPAM_BIN="$OPAM_BIN" OLLY_DIR="$OLLY_DIR" \
    PYTHONPATH="$ROOT_DIR/src" python3 -m running.switches ensure \
        --compiler "$OCAML_VERSION"

step "Pre-installing benchmark packages in $OPAM_SWITCH"

BUILD_TOOLS=(
    dune
    ocamlfind
    opam-compiler   # provisions runtime switches via `opam compiler create`
    processor   # ocaml-processor-dump topology for CpuPin and the manifest; optional
)

# olly's deps are not installed here: they would evict opam-compiler (cmdliner
# conflict). olly's own switch resolves them from its opam file.

# Pre-warm only: the ~/benches build scripts install their own deps; this speeds up first runs.
BENCH_PKGS=(
    domainslib
    zarith num              # zarith, benchmarksgame (pidigits5, binarytrees5)
    lwt                     # chameneos, thread-lwt
    decompress              # test_decompress
    bigstringaf checkseum   # decompress deps
    yojson camlp-streams    # ydump
    str                     # benchmarksgame (fasta, spectralnorm)
)

# BUILD_TOOLS are provisioned by running.switches; only the pre-warm is left.
ALL_PKGS=("${BENCH_PKGS[@]}")

echo "  Installing: ${ALL_PKGS[*]}"
"$OPAM_BIN" install --switch="$OPAM_SWITCH" --yes "${ALL_PKGS[@]}"

ok "OCaml packages installed"

# opam compiler plugin
step "Verifying the opam compiler plugin"
# running.switches registers the link; this checks it resolves. Without it `opam compiler
# create` dies with "unknown command 'compiler'" (no tty to prompt). No --switch: the
# invalid source is rejected in argument parsing, so nothing can be created.
if "$OPAM_BIN" compiler create "invalid/source#nope" </dev/null 2>&1 \
        | grep -q "unknown command"; then
    red "ERROR: the opam 'compiler' plugin does not resolve, so runtime"
    red "switches cannot be provisioned and no sweep can run."
    red "Try: PYTHONPATH=$ROOT_DIR/src python3 -m running.switches status"
    exit 1
fi
ok "opam compiler plugin resolves"

# 4. olly
step "Building runtime_events_tools (olly)"

if [[ ! -d "$OLLY_DIR" ]]; then
    echo "  Cloning runtime_events_tools..."
    git clone https://github.com/tarides/runtime_events_tools.git "$OLLY_DIR"
fi

pushd "$OLLY_DIR" >/dev/null


# No --set-switch: an early exit would leave the user's global switch changed.
eval "$("$OPAM_BIN" env --switch="$OLLY_SWITCH")"
# Not piped to `tail`, which would report tail's exit status rather than dune's.
BUILD_LOG="${TMPDIR:-/tmp}/running-ng-olly-build.log"
if ! dune build -p runtime_events_tools -j "$(nproc)" @install > "$BUILD_LOG" 2>&1; then
    red "ERROR: the olly build failed. Last 30 lines of $BUILD_LOG:"
    tail -30 "$BUILD_LOG"
    popd >/dev/null
    exit 1
fi

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

# 5. Python dependencies
step "Installing Python dependencies"

pip3 install --user --quiet pyyaml 2>/dev/null || pip3 install --quiet pyyaml
ok "pyyaml installed"

# 6. Benchmarks
step "Checking benchmarks directory"

if [[ -d "$BENCHES_DIR" ]]; then
    ok "Benchmarks found at $BENCHES_DIR"
else
    echo "  Cloning benches repo to $BENCHES_DIR..."
    git clone https://github.com/udesou/benches.git "$BENCHES_DIR"
    ok "Benchmarks cloned to $BENCHES_DIR"
fi

# 7. Verify
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

eval "$("$OPAM_BIN" env --switch="$OPAM_SWITCH" --set-switch)"

echo "  System commands:"
check_cmd python3
check_cmd git
check_cmd autoconf
check_cmd make
check_cmd gcc
check_cmd "$OPAM_BIN"

echo "  perf (non-fatal if missing):"
if command -v perf &>/dev/null; then
    ok "perf"
else
    warn "perf not found — PerfAndOllyAttach modifiers will not work"
fi

echo "  OCaml tools (from switch $OPAM_SWITCH):"
check_cmd dune
check_cmd ocamlfind
check_cmd ocamlopt

echo "  Files:"
check_file "$OLLY_EXE"
check_file "$BENCHES_DIR"
check_file "$ROOT_DIR/src/running/config/examples/ocaml_gc_sweep_example.yml"

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
    echo "  - The first run will take longer as it builds OCaml/OxCaml runtimes."
    echo "    Subsequent runs reuse cached toolchains in /tmp/running-ng-ocaml-toolchains/."
    echo "  - Edit the config file to enable/disable benchmark suites:"
    echo "    $ROOT_DIR/src/running/config/examples/ocaml_gc_sweep_example.yml"
    echo "  - The opam switch '$OPAM_SWITCH' should be active when running benchmarks"
    echo "    that need dune/ocamlfind (with_packages, with_deps, multicore suites)."
    echo "    Running: eval \$($OPAM_BIN env --switch=$OPAM_SWITCH --set-switch)"
else
    red "$ERRORS dependency check(s) failed — see above for details."
    exit 1
fi
