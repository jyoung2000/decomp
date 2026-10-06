#!/usr/bin/env bash
# Deterministic build of the pecli fixture: original/pecli.exe, README.txt, sample.dat + manifest.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
rm -rf original original.manifest.json
mkdir -p original
x86_64-w64-mingw32-gcc -O1 -s -Wall -Wextra \
  -Wl,--no-insert-timestamp -Wl,--build-id=none \
  -o original/pecli.exe src/pecli.c
cp src/README.txt original/README.txt
python3 src/make_sample.py original/sample.dat
touch -d @0 original/pecli.exe original/README.txt original/sample.dat
python3 ../tools/make_manifest.py pecli original original.manifest.json
echo "pecli: built $(sha256sum original/pecli.exe | cut -c1-16)... ($(stat -c %s original/pecli.exe) bytes)"
