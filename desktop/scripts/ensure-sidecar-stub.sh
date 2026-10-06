#!/usr/bin/env sh
# tauri-build refuses to compile when `bundle.externalBin` points at a missing file.
# For cargo check/test/dev on a machine without a packaged controller, create a tiny stub.
# The shell recognises stubs (< 64 KiB) and falls back to `python -m rebuild_controller.cli.main serve`
# in debug builds. NEVER ship a stub: Build-RebuildStudio.ps1 replaces it with the real binary.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
triple=${1:-$(rustc -vV | sed -n 's/^host: //p')}
ext=""
case "$triple" in *windows*) ext=".exe" ;; esac
target="$here/src-tauri/binaries/rebuild-controller-$triple$ext"
mkdir -p "$here/src-tauri/binaries"
if [ ! -s "$target" ]; then
  printf '#!/bin/sh\necho "rebuild-controller dev stub: build the real sidecar with scripts/windows/Build-RebuildStudio.ps1" >&2\nexit 3\n' > "$target"
  chmod +x "$target"
  echo "created stub $target"
fi
