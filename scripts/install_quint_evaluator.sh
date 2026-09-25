#!/usr/bin/env bash
# Pre-seed the Quint Rust evaluator binary into its cache.
#
# `quint test` / `quint run` need a `quint_evaluator` binary, which the CLI
# downloads on first use from the GitHub releases *API* — unauthenticated. On
# shared CI runner IPs that call is routinely rate-limited (HTTP 403, "rate
# limit exceeded"), so the specs job dies before a single spec runs.
#
# GitHub's direct release-asset URLs (release-assets.githubusercontent.com)
# carry no such limit, so we fetch the asset ourselves and lay it out exactly
# where the CLI expects it. The CLI then finds it already present and skips its
# own download. Idempotent: a pre-existing binary short-circuits.
#
# Usage: scripts/install_quint_evaluator.sh
# Env:   QUINT_HOME            quint cache root (default: ~/.quint)
#        QUINT_PACKAGE_ROOT   installed @informalsystems/quint dir
#                             (default: `npm root -g`/@informalsystems/quint)
set -euo pipefail

quint_root="${QUINT_PACKAGE_ROOT:-$(npm root -g)/@informalsystems/quint}"
if [ ! -f "$quint_root/dist/src/rust/binaryManager.js" ]; then
  echo "install_quint_evaluator: @informalsystems/quint not found at $quint_root" >&2
  echo "install it first (e.g. npm install -g @informalsystems/quint)" >&2
  exit 1
fi

# The evaluator version is the CLI's own contract with the release it fetches,
# so read it from the package rather than pinning a copy that can drift.
version="$(node -p "require('$quint_root/dist/src/rust/binaryManager.js').QUINT_EVALUATOR_VERSION")"

case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) asset='quint_evaluator-x86_64-unknown-linux-gnu.tar.gz' ;;
  Linux/aarch64 | Linux/arm64) asset='quint_evaluator-aarch64-unknown-linux-gnu.tar.gz' ;;
  Darwin/arm64) asset='quint_evaluator-aarch64-apple-darwin.tar.gz' ;;
  Darwin/x86_64) asset='quint_evaluator-x86_64-apple-darwin.tar.gz' ;;
  *)
    echo "install_quint_evaluator: unsupported platform $(uname -s)/$(uname -m)" >&2
    exit 1
    ;;
esac

dir="${QUINT_HOME:-$HOME/.quint}/rust-evaluator-$version"
if [ -x "$dir/quint_evaluator" ]; then
  echo "install_quint_evaluator: already present at $dir/quint_evaluator"
  exit 0
fi

url="https://github.com/informalsystems/quint/releases/download/evaluator/$version/$asset"
echo "install_quint_evaluator: fetching $asset ($version) into $dir"
mkdir -p "$dir"
# Download to a temp file first so a failed transfer never leaves a partial
# executable that the `-x` short-circuit above would then trust.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 --retry-all-errors -o "$tmp" "$url"
tar -xzf "$tmp" -C "$dir"
chmod +x "$dir/quint_evaluator"
echo "install_quint_evaluator: installed $dir/quint_evaluator"
