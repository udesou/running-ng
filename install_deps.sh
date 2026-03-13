#!/usr/bin/env bash
# install_deps.sh — Auto-detect OS and run the appropriate install script.
#
# Usage:
#   bash ~/running-ng/install_deps.sh
#
# Delegates to:
#   - install_deps_linux.sh  (Ubuntu/Debian)
#   - install_deps_macos.sh  (macOS)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
    Linux)
        exec bash "$ROOT_DIR/install_deps_linux.sh" "$@"
        ;;
    Darwin)
        exec bash "$ROOT_DIR/install_deps_macos.sh" "$@"
        ;;
    *)
        echo "ERROR: Unsupported OS: $(uname -s)" >&2
        echo "Supported: Linux (Ubuntu/Debian), macOS" >&2
        exit 1
        ;;
esac
