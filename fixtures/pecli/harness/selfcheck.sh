#!/usr/bin/env bash
# Positive and negative controls for the pecli oracle (Linux only, no wine needed):
#  - the original C source compiled natively must PASS expected/scenarios.json
#  - wrong_remake (Rust) must FAIL it
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
gcc -O1 -o "$T/pecli_native" "$HERE/src/pecli.c"
echo "== positive control (native build of original source)"
python3 "$HERE/../tools/scenario_runner.py" check --fixture "$HERE" --launcher "$T/pecli_native"
echo "== negative control (wrong_remake must be rejected)"
CARGO_TARGET_DIR="$T/target" cargo build --release --offline --manifest-path "$HERE/wrong_remake/Cargo.toml" 2>&1 | tail -1
if python3 "$HERE/../tools/scenario_runner.py" check --fixture "$HERE" --launcher "$T/target/release/pecli"; then
  echo "ERROR: wrong_remake was accepted"; exit 1
else
  echo "OK: wrong_remake rejected"
fi
