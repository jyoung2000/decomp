#!/usr/bin/env sh
# Linux validation of the Rust shell: stub sidecar + placeholder frontend when ui/dist is absent.
#   desktop/scripts/check.sh            -> cargo check
#   desktop/scripts/check.sh test       -> cargo test
#   desktop/scripts/check.sh build      -> cargo build
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
"$here/scripts/ensure-sidecar-stub.sh"
cd "$here/src-tauri"
if [ ! -f "$here/../ui/dist/index.html" ]; then
  export TAURI_CONFIG='{"build":{"frontendDist":"../placeholder-dist"}}'
  echo "ui/dist missing: using desktop/placeholder-dist (check/test only)" >&2
fi
cmd=check
if [ $# -gt 0 ]; then
  cmd=$1
  shift
fi
exec cargo "$cmd" --locked "$@"
