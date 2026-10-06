#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
uv sync --locked
uv run pytest -p no:cacheprovider
nix flake check
