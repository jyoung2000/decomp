#!/usr/bin/env bash
# Deterministic build of the godotgame fixture: original/game.pck (+ README, manifest).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
rm -rf original original.manifest.json
mkdir -p original
python3 src/pack_pck.py pack src original/game.pck --godot 4.3.0 --align 32
python3 src/pack_pck.py verify original/game.pck src
cat > original/godotgame.x86_64.README <<'TXT'
godotgame.x86_64 is the Godot 4.3 export template / engine executable that would normally sit next to
game.pck. The engine binary is NOT redistributed with this fixture (licence/size; Rebuild Studio only
ever analyses game.pck). This file is a placeholder so the install root has the usual two-file shape.

game.pck : Godot PCK, pack format 2, engine version fields 4.3.0, 7 entries (see ../README.md).
TXT
find original -type f -exec touch -d @0 {} +
python3 ../tools/make_manifest.py godotgame original original.manifest.json
echo "godotgame: built $(ls original | tr '\n' ' ')"
