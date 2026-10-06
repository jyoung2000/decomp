#!/usr/bin/env bash
# Deterministic build of the webapp fixture:
#   original/            deployable static site (the web files of src/)
#   original-electron/   Electron-style package: resources/app.asar (+ placeholder for the missing binary)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
WEB_FILES="index.html app.js styles.css manifest.webmanifest sw.js icon.svg"

rm -rf original original-electron original.manifest.json original-electron.manifest.json
mkdir -p original original-electron/resources

# 1. deployable site
for f in $WEB_FILES; do cp "src/$f" "original/$f"; done

# 2. pinned asar tool (harness/package.json + package-lock.json pin @electron/asar)
( cd harness && [ -x node_modules/.bin/asar ] || PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --no-audit --no-fund >/dev/null )

# 3. app.asar = same web app + main.js + package.json (mtimes are not stored in asar; order is sorted)
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
mkdir "$STAGE/app"
for f in $WEB_FILES; do cp "src/$f" "$STAGE/app/$f"; done
cp src/electron/main.js src/electron/package.json "$STAGE/app/"
harness/node_modules/.bin/asar pack "$STAGE/app" original-electron/resources/app.asar

cat > original-electron/ELECTRON_BINARY_MISSING.txt <<'TXT'
Placeholder: the Electron runtime (electron.exe / electron, *.dll, *.pak, locales/, ...) is NOT included in
this fixture; only the application payload resources/app.asar is shipped, exactly as an Electron app's
resources/ folder would contain it. Rebuild Studio analyses app.asar; it never needs to launch Electron.
Package metadata: name pocket-notes-electron 1.0.0, main = main.js (see package.json inside the asar).
TXT

find original original-electron -type f -exec touch -d @0 {} +
python3 ../tools/make_manifest.py webapp original original.manifest.json
python3 ../tools/make_manifest.py webapp-electron original-electron original-electron.manifest.json
echo "webapp: built original/ ($(ls original | wc -l) files) and original-electron/ (app.asar $(stat -c %s original-electron/resources/app.asar) bytes)"
