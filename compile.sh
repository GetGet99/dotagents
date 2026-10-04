#!/usr/bin/env bash
# Compile rules/*.mdc into autogen-*.md files and Cursor symlinks.
# Thin wrapper around scripts/compile.py — all args are passed through.
#
# Usage:
#   ./compile.sh                  # all enabled platforms
#   ./compile.sh --check          # CI: fail if outputs differ
#   ./compile.sh --platform cursor
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/scripts/compile.py" --repo-root "$ROOT" "$@"
