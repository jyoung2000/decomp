#!/usr/bin/env bash
# Build every fixture original, verify reproducibility against fixtures/manifest.json and run the fast oracle self-checks.
#   fixtures/build_all.sh            build + verify (no wine needed; ~1 min; javacli needs a JDK 17 on PATH/JAVA_HOME)
#   fixtures/build_all.sh --regen    additionally re-record all expected/ files (needs wine, dotnet, chromium; ~3 min)
# Any failing step aborts with a non-zero status.
set -euo pipefail
FX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 PYTHONDONTWRITEBYTECODE=1
export WINEDEBUG=-all WINEPREFIX="${WINEPREFIX:-/opt/rebuild-tools/wineprefix}"
REGEN=0; [ "${1:-}" = "--regen" ] && REGEN=1

step() { echo; echo "=== $*"; }

for f in pecli dotnetapp javacli godotgame webapp; do
  step "build $f"; "$FX/$f/build.sh"
done

if [ "$REGEN" = 1 ]; then
  step "regen pecli expected (wine)";     (cd "$FX/pecli" && python3 ../tools/scenario_runner.py generate --fixture .)
  step "regen dotnetapp expected";        (cd "$FX/dotnetapp" && python3 ../tools/scenario_runner.py generate --fixture .)
  step "regen javacli expected (java)";        (cd "$FX/javacli" && python3 ../tools/scenario_runner.py generate --fixture .)
  step "regen godotgame expected (GDRE)"; python3 "$FX/godotgame/harness/gen_expected.py"
  step "regen webapp expected (playwright)"; (cd "$FX/webapp/harness" && npm test)
  step "refresh manifest.json"; python3 "$FX/tools/make_fixtures_manifest.py"
fi

step "manifest verification (reproducible originals)"; python3 "$FX/tools/make_fixtures_manifest.py" --verify
step "pecli: positive control passes, wrong_remake rejected"; "$FX/pecli/harness/selfcheck.sh"
step "dotnetapp: original replays its own oracle"; "$FX/dotnetapp/harness/selfcheck.sh"
step "javacli: original replays its own oracle (java)"; "$FX/javacli/harness/selfcheck.sh"
step "godotgame: pck verifies against src"; python3 "$FX/godotgame/src/pack_pck.py" verify "$FX/godotgame/original/game.pck" "$FX/godotgame/src"
step "webapp: original replays its own oracle (playwright)"; (cd "$FX/webapp/harness" && npm run --silent check >/dev/null && echo "webapp check: no scenario diffs")
echo; echo "build_all: OK"
