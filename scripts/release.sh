#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
version="${1:?Usage: scripts/release.sh <version>}"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
actual="$(python3 -c 'import tomllib; d=tomllib.load(open("Cargo.toml","rb")); print(d["package"]["version"])')"
[[ "$actual" == "$version" ]] || {
  echo "Cargo version $actual differs from release $version" >&2
  exit 1
}
cargo build --locked --release --workspace
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/bin" dist
install -m755 target/release/bilibili-live-helper "$stage/bin/"
"$stage/bin/bilibili-live-helper" --version | grep -Fx "bilibili-live-helper $version"
printf '%s\n' "$version" >"$stage/VERSION"
archive="bilibili-live-helper-$version-x86_64-linux.tar.gz"
tar -C "$stage" -czf "dist/$archive" .
(cd dist && sha256sum "$archive" >"$archive.sha256")
