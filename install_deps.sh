#!/usr/bin/env bash
# Detect the OS and exec the matching install_deps_<os>.sh. Usage: bash install_deps.sh

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
        # sh, not bash: FreeBSD base has no bash and nothing is installed yet.
        exec sh "$ROOT_DIR/install_deps_freebsd.sh" "$@"
        ;;
    *)
        echo "ERROR: Unsupported OS: $(uname -s)" >&2
        echo "Supported: Linux (Ubuntu/Debian), macOS, FreeBSD" >&2
        exit 1
        ;;
esac
