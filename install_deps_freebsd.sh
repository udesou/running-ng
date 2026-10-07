#!/bin/sh
# Prepare a FreeBSD host to run running-ng, assuming NO ROOT: nothing is pkg-installed,
# missing packages are reported for someone with privileges. Installs a user-local opam,
# a sandbox-free opam root, the running-ng tools switch, and clones the two benchmark
# repos beside this one. Benchmark runtimes and olly are not built here; running-ng
# builds them per config.
# Set BENCHES_DIR / MACRO_BENCHES_DIR to use checkouts you already have;
# an existing directory is never touched.
# Usage: sh install_deps_freebsd.sh [--check]   (--check reports and changes nothing)
# POSIX sh: FreeBSD base has no bash.

set -eu

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
OPAM_VERSION="${OPAM_VERSION:-2.5.2}"
OPAM_SWITCH="${OPAM_SWITCH:-running-ng-tools}"
OCAML_VERSION="${OCAML_VERSION:-5.4.0}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PARENT_DIR=$(cd "$ROOT_DIR/.." && pwd)
BENCHES_DIR="${BENCHES_DIR:-$PARENT_DIR/benches}"
MACRO_BENCHES_DIR="${MACRO_BENCHES_DIR:-$PARENT_DIR/macro-benches}"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }
step()  { blue "==> $*"; }
ok()    { green "    OK: $*"; }
have()  { command -v "$1" >/dev/null 2>&1; }

# Clone at its default branch, or leave an existing checkout alone: it may be a
# pinned one (the bench service points the bench dirs at its own).
clone_if_missing() {
    if [ -d "$2" ]; then
        ok "$(basename "$2") at $2"
    else
        echo "  cloning $1 into $2 ..."
        git clone --quiet "$1" "$2"
        ok "$(basename "$2") cloned to $2"
    fi
}

if [ "$(uname -s)" != "FreeBSD" ]; then
    red "ERROR: this script is for FreeBSD. Use install_deps.sh, which dispatches."
    exit 1
fi

# 1. System prerequisites (cannot install without root)
step "Checking system prerequisites (cannot install these without root)"

BLOCKED=""
need() {
    if have "$1"; then
        ok "$1"
    else
        red "    MISSING: $1  ($2)"
        BLOCKED="$BLOCKED $3"
    fi
}

need git  "needed to fetch olly and the benchmarks"        git
need cc   "FreeBSD base clang; needed to build OCaml"      llvm
need make "needed to build OCaml"                          gmake
need curl "needed to download opam"                        curl
need unzip "opam extracts some archives with it"           unzip

# Absence silently removes benchmarks rather than failing loudly.
step "Checking optional libraries (absence disables specific benchmarks)"
if [ -e /usr/local/include/gmp.h ] || [ -e /usr/include/gmp.h ]; then
    ok "gmp headers"
else
    warn "gmp headers absent: zarith will not build, which removes every"
    warn "benchmark depending on it (pidigits5, binarytrees5, ...). Needs"
    warn "root: pkg install gmp"
    BLOCKED="$BLOCKED gmp"
fi
if have pkgconf || have pkg-config; then
    ok "pkg-config"
else
    warn "pkg-config absent: dune cannot find C libraries, so some packages"
    warn "will mis-detect features. Needs root: pkg install pkgconf"
    BLOCKED="$BLOCKED pkgconf"
fi
if have cmake; then
    ok "cmake"
else
    warn "cmake absent: olly's hdr_histogram will not build, so configs that"
    warn "attach olly (perf_grp*) stop before running. Needs root: pkg install cmake"
    BLOCKED="$BLOCKED cmake"
fi
if pkg info -e libffi 2>/dev/null; then
    ok "libffi"
else
    warn "libffi absent: olly's ctypes-foreign will not build, so configs that"
    warn "attach olly (perf_grp*) stop before running. Needs root: pkg install libffi"
    BLOCKED="$BLOCKED libffi"
fi

step "Checking the opam compiler plugin"
# runtime.py runs `opam compiler create`, which resolves opam-compiler as a plugin from
# $(opam var root)/plugins/bin, not from the switch it was installed into.
if have opam; then
    if opam compiler create "invalid/source#nope" </dev/null 2>&1 \
            | grep -q "unknown command"; then
        warn "the opam 'compiler' plugin does not resolve, so no runtime switch"
        warn "can be provisioned and no sweep can run. A full run of this"
        warn "script registers it; that is what the plugin step below does."
    else
        ok "opam compiler plugin resolves"
    fi
else
    echo "  (no opam yet; the plugin is registered during the install)"
fi

step "Checking hwpmc (needed for hardware counters)"
if kldstat -m hwpmc >/dev/null 2>&1; then
    ok "hwpmc loaded"
    UNPRIV=$(sysctl -n security.bsd.unprivileged_proc_debug 2>/dev/null || echo "?")
    if [ "$UNPRIV" = "1" ]; then
        ok "security.bsd.unprivileged_proc_debug=1 (process PMCs need no root)"
    else
        warn "security.bsd.unprivileged_proc_debug=$UNPRIV: attaching PMCs to"
        warn "your own processes will fail. Needs root to set to 1."
    fi
else
    warn "hwpmc not loaded, so there will be no hardware counters. running-ng"
    warn "degrades to the 'none' backend (olly and rusage still work)."
    warn "Needs root: kldload hwpmc, or hwpmc_load=\"YES\" in /boot/loader.conf"
fi

if [ -n "$BLOCKED" ]; then
    echo ""
    red "Blocked on packages that need root:$BLOCKED"
    red "Ask someone with privileges to run: pkg install$BLOCKED"
    # Only the hard prerequisites are fatal; gmp/pkgconf only cost benchmarks.
    for pkg in $BLOCKED; do
        case "$pkg" in
            gmp|pkgconf|cmake|libffi) ;;
            *) red "Cannot continue without $pkg."; exit 1 ;;
        esac
    done
    warn "Continuing anyway; the affected benchmarks will not build."
fi

if [ "$CHECK_ONLY" = "1" ]; then
    echo ""
    step "Check-only mode, stopping here. Re-run without --check to install."
    exit 0
fi

# 2. opam, user-local
step "Ensuring opam >= 2.2"

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

OPAM_BIN=""
if have opam && version_ge "$(opam --version)" "2.2.0"; then
    OPAM_BIN=$(command -v opam)
    ok "using existing opam $(opam --version) at $OPAM_BIN"
elif [ -x "$LOCAL_BIN/opam" ] && version_ge "$("$LOCAL_BIN/opam" --version)" "2.2.0"; then
    OPAM_BIN="$LOCAL_BIN/opam"
    ok "using existing opam $("$OPAM_BIN" --version) at $OPAM_BIN"
else
    # The official installer writes to /usr/local/bin (root); fetch the static binary instead.
    URL="https://github.com/ocaml/opam/releases/download/$OPAM_VERSION/opam-$OPAM_VERSION-x86_64-freebsd"
    echo "  downloading $URL"
    mkdir -p "$LOCAL_BIN"
    curl -fsSL "$URL" -o "$LOCAL_BIN/opam.tmp"
    chmod +x "$LOCAL_BIN/opam.tmp"
    # No signature check: gpg is not in FreeBSD base. HTTPS from a pinned version; the
    # .sig is published next to the binary.
    GOT=$("$LOCAL_BIN/opam.tmp" --version 2>/dev/null || echo "")
    if [ "$GOT" != "$OPAM_VERSION" ]; then
        rm -f "$LOCAL_BIN/opam.tmp"
        red "ERROR: downloaded opam reports version '$GOT', expected $OPAM_VERSION"
        exit 1
    fi
    mv "$LOCAL_BIN/opam.tmp" "$LOCAL_BIN/opam"
    OPAM_BIN="$LOCAL_BIN/opam"
    ok "installed opam $OPAM_VERSION to $OPAM_BIN"
    case ":$PATH:" in
        *":$LOCAL_BIN:"*) ;;
        *) warn "$LOCAL_BIN is not on PATH. Add it: export PATH=\"$LOCAL_BIN:\$PATH\"" ;;
    esac
fi

# 3. opam root
step "Initialising the opam root"
if [ -d "${OPAMROOT:-$HOME/.opam}" ]; then
    ok "opam root already exists at ${OPAMROOT:-$HOME/.opam}"
else
    # bubblewrap is Linux-only and opam's FreeBSD sandbox needs privileges we lack.
    "$OPAM_BIN" init --bare --yes --disable-sandboxing
    ok "opam root initialised (sandboxing off)"
fi

# 4. Switches
step "Provisioning running-ng's opam switches"
# running.switches is the single declaration of the tools switch; it creates or
# rebuilds it and restores the active switch.
# Stdlib-only, so plain python3 works before running-ng is installed anywhere.
OPAM_BIN="$OPAM_BIN" \
    PYTHONPATH="$ROOT_DIR/src" python3 -m running.switches ensure \
        --compiler "$OCAML_VERSION"
# The rest concerns running-ng's own switches, which live in its own opam root.
export OPAMROOT="$(PYTHONPATH="$ROOT_DIR/src" python3 -m running.switches root)"

# Optional: ocaml-processor-dump gives P/E-core and socket topology to CpuPin and the
# manifest; running-ng falls back to the kernel view, so a failure must not abort.
step "Installing optional topology tooling"
if "$OPAM_BIN" install --switch="$OPAM_SWITCH" --yes processor; then
    ok "processor installed"
else
    warn "processor failed to install"
    warn "continuing; running-ng falls back to the kernel's own topology view."
fi

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

# 5. Benchmarks
step "Checking the benchmark repositories"
clone_if_missing https://github.com/ocaml-bench/benches.git "$BENCHES_DIR"
clone_if_missing https://github.com/ocaml-bench/macro-benches.git "$MACRO_BENCHES_DIR"

# 6. Summary
echo ""
step "Done. To use this environment:"
echo "  export PATH=\"$LOCAL_BIN:\$PATH\""
echo "  export OPAMROOT=$OPAMROOT; eval \$($OPAM_BIN env --switch=$OPAM_SWITCH --set-switch)"
echo ""
echo "Before the first macro run, vendor macro-benches' dependencies (slow, once):"
echo "  make -C $MACRO_BENCHES_DIR setup"
echo ""
echo "Then check what running-ng makes of the host:"
echo "  sh $ROOT_DIR/scripts/portability_probe.sh"
if [ -n "$BLOCKED" ]; then
    echo ""
    warn "Still blocked on root for:$BLOCKED"
fi
