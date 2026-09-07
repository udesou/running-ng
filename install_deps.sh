#!/usr/bin/env bash
# install_deps.sh — Auto-detect OS and run the appropriate install script.
#
# Usage:
#   bash ~/running-ng/install_deps.sh
#
# Delegates to:
#   - install_deps_linux.sh    (Ubuntu/Debian)
#   - install_deps_macos.sh    (macOS)
#   - install_deps_freebsd.sh  (FreeBSD; assumes no root, see its header)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
    Linux)
        exec bash "$ROOT_DIR/install_deps_linux.sh" "$@"
        ;;
    Darwin)
        exec bash "$ROOT_DIR/install_deps_macos.sh" "$@"
        ;;
    FreeBSD)
        # sh, not bash: FreeBSD has no bash in the base system, and this
        # script must run before anything has been installed.
        exec sh "$ROOT_DIR/install_deps_freebsd.sh" "$@"
        ;;
    *)
        echo "ERROR: Unsupported OS: $(uname -s)" >&2
        echo "Supported: Linux (Ubuntu/Debian), macOS, FreeBSD" >&2
        exit 1
        ;;
esac
