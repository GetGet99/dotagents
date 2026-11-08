#!/usr/bin/env bash
# Thin wrapper around the dotagents CLI for repo-local development.
# Usage: ./compile.sh [build args...]
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m dotagents.cli build "$@"
