#!/bin/sh
# install_deps_freebsd.sh - prepare a FreeBSD host to run running-ng.
#
# Usage:
#   sh install_deps_freebsd.sh --check     report what is present and what is
#                                          missing; changes nothing
#   sh install_deps_freebsd.sh             do the work
#
# Differs from the Linux and macOS scripts in one way that shapes everything:
# it assumes NO ROOT. There is no `pkg install` here and no sudo. Anything that
# genuinely needs a package installed is reported for a human with privileges
# to deal with, rather than attempted and failed halfway through.
#
# What it does:
#   1. Checks the system prerequisites it cannot install.
#   2. Installs opam as a user-local binary (the project ships a static FreeBSD
#      amd64 build) unless a good enough one is already on PATH.
#   3. Initialises a user-local opam root with sandboxing off (bubblewrap is
#      Linux-only, and opam's FreeBSD sandbox needs privileges we do not have).
#   4. Creates the running-ng tools switch and installs the harness's own
#      OCaml dependencies into it.
#   5. Builds olly (runtime_events_tools) from source.
#
# What it deliberately does NOT do: build the benchmark runtimes. running-ng
# provisions those itself, per config, via `opam compiler create`.
#
# POSIX sh on purpose: FreeBSD has no bash in the base system.

set -eu

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
OPAM_VERSION="${OPAM_VERSION:-2.5.2}"
OPAM_SWITCH="${OPAM_SWITCH:-running-ng-tools}"
OCAML_VERSION="${OCAML_VERSION:-5.4.0}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
OLLY_DIR="${OLLY_DIR:-$HOME/runtime_events_tools}"
BENCHES_DIR="${BENCHES_DIR:-$(cd "$ROOT_DIR/.." && pwd)/benches}"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }
step()  { blue "==> $*"; }
ok()    { green "    OK: $*"; }
have()  { command -v "$1" >/dev/null 2>&1; }

if [ "$(uname -s)" != "FreeBSD" ]; then
    red "ERROR: this script is for FreeBSD. Use install_deps.sh, which dispatches."
    exit 1
fi

# =============================================================================
# 1. System prerequisites we cannot install ourselves
# =============================================================================
step "Checking system prerequisites (cannot install these without root)"

BLOCKED=""
NO_CMAKE=0
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

# Optional, but their absence silently removes benchmarks rather than failing
# loudly, so they are worth naming up front.
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
    warn "cmake absent: hdr_histogram will not build (it depends on conf-cmake),"
    warn "and without hdr_histogram olly has no gc-stats subcommand -- which is"
    warn "exactly what running-ng invokes. Hardware counters and rusage still"
    warn "work, but there will be no GC metrics. Needs root: pkg install cmake"
    BLOCKED="$BLOCKED cmake"
    NO_CMAKE=1
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
            gmp|pkgconf|cmake) ;;
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

# =============================================================================
# 2. opam, user-local
# =============================================================================
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
    # The official installer writes to /usr/local/bin, which needs root, so
    # fetch the static binary straight into a user-local prefix instead.
    URL="https://github.com/ocaml/opam/releases/download/$OPAM_VERSION/opam-$OPAM_VERSION-x86_64-freebsd"
    echo "  downloading $URL"
    mkdir -p "$LOCAL_BIN"
    curl -fsSL "$URL" -o "$LOCAL_BIN/opam.tmp"
    chmod +x "$LOCAL_BIN/opam.tmp"
    # Signature verification is skipped: gpg is not in FreeBSD base and we
    # cannot install it. The download is over HTTPS from a pinned version. If
    # you have gpg, the matching .sig is published next to the binary.
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

# =============================================================================
# 3. opam root
# =============================================================================
step "Initialising the opam root"
if [ -d "${OPAMROOT:-$HOME/.opam}" ]; then
    ok "opam root already exists at ${OPAMROOT:-$HOME/.opam}"
else
    # --disable-sandboxing: bubblewrap is Linux-only, and opam's FreeBSD
    # sandbox wants privileges we do not have.
    "$OPAM_BIN" init --bare --yes --disable-sandboxing
    ok "opam root initialised (sandboxing off)"
fi

# =============================================================================
# 4. Tools switch
# =============================================================================
step "Ensuring the $OPAM_SWITCH switch (OCaml $OCAML_VERSION)"
if "$OPAM_BIN" switch list --short 2>/dev/null | grep -qx "$OPAM_SWITCH"; then
    ok "switch $OPAM_SWITCH already exists"
else
    echo "  creating it; this compiles a compiler and takes a while"
    "$OPAM_BIN" switch create "$OPAM_SWITCH" "ocaml-base-compiler.$OCAML_VERSION" --yes
fi

step "Installing the harness's OCaml dependencies"
# dune/ocamlfind: build tools most benchmark build scripts expect on PATH.
# opam-compiler: `opam compiler create` provisions every runtime switch
#   (runtime.py); without it a run dies with `unknown command 'compiler'`.
# cmdliner/hdr_histogram/trace/trace-fuchsia: olly's dependencies.
# processor: ocaml-processor-dump, which supplies P/E-core and socket topology
#   for CpuPin and the run manifest. Optional; running-ng falls back to the
#   kernel's own view without it.
# Split deliberately: under `set -e` a single failing package used to abort the
# script before olly was built and the benchmarks were fetched.
# Required: build tools plus olly's non-cmake dependencies.
PKGS_REQUIRED="dune ocamlfind opam-compiler cmdliner trace trace-fuchsia"
# gc-stats only: hdr_histogram pulls conf-cmake, needing a system cmake we
# cannot install without root. Skipped rather than fatal.
PKGS_GCSTATS="hdr_histogram"
# Optional: supplies P/E-core and socket topology. running-ng falls back to the
# kernel's own view, so a failure here must not stop the run.
PKGS_OPTIONAL="processor"

"$OPAM_BIN" install --switch="$OPAM_SWITCH" --yes $PKGS_REQUIRED

if [ "$NO_CMAKE" = "1" ]; then
    warn "skipping $PKGS_GCSTATS: no system cmake (see the check above)"
else
    "$OPAM_BIN" install --switch="$OPAM_SWITCH" --yes $PKGS_GCSTATS
fi

if "$OPAM_BIN" install --switch="$OPAM_SWITCH" --yes $PKGS_OPTIONAL; then
    :
else
    warn "optional packages failed: $PKGS_OPTIONAL"
    warn "continuing; running-ng falls back to the kernel's own topology view."
fi

# =============================================================================
# 5. olly
# =============================================================================
step "Building olly (runtime_events_tools)"
if [ "$NO_CMAKE" = "1" ]; then
    warn "SKIPPED: olly needs hdr_histogram, which needs a system cmake."
    warn "Everything else is installed. Get cmake installed (needs root) and"
    warn "re-run this script; the switch and packages above will be reused."
    OLLY_EXE=""
else
if [ ! -d "$OLLY_DIR" ]; then
    git clone https://github.com/tarides/runtime_events_tools.git "$OLLY_DIR"
fi
NCPU=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
( cd "$OLLY_DIR" && eval "$("$OPAM_BIN" env --switch="$OPAM_SWITCH")" && \
  dune build -p runtime_events_tools -j "$NCPU" @install 2>&1 | tail -5 )
OLLY_EXE="$OLLY_DIR/_build/install/default/bin/olly"
if [ -x "$OLLY_EXE" ]; then
    ok "olly built at $OLLY_EXE"
else
    red "ERROR: olly not found at $OLLY_EXE after the build"
    exit 1
fi
fi

# =============================================================================
# 6. Benchmarks
# =============================================================================
step "Checking the benchmarks directory"
if [ -d "$BENCHES_DIR" ]; then
    ok "benchmarks at $BENCHES_DIR"
else
    git clone https://github.com/udesou/benches.git "$BENCHES_DIR"
    ok "benchmarks cloned to $BENCHES_DIR"
fi

# =============================================================================
# 7. Summary
# =============================================================================
echo ""
step "Done. To use this environment:"
echo "  export PATH=\"$LOCAL_BIN:\$PATH\""
echo "  eval \$($OPAM_BIN env --switch=$OPAM_SWITCH --set-switch)"
if [ -n "$OLLY_EXE" ]; then
    echo "  export PATH=\"$OLLY_DIR/_build/install/default/bin:\$PATH\""
fi
echo ""
echo "Then check what running-ng makes of the host:"
echo "  sh $ROOT_DIR/scripts/portability_probe.sh"
if [ -n "$BLOCKED" ]; then
    echo ""
    warn "Still blocked on root for:$BLOCKED"
fi
