#!/usr/bin/env bash
set -euo pipefail
app="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fleet="${INFRA_WORKSPACE:?Enter the infra root nix develop shell}/nixos-server-fleet"
nix build --no-link "$fleet#checks.x86_64-linux.bilibili-live-helper" --override-input bilibili-live-helper "git+file://$app" --no-write-lock-file
