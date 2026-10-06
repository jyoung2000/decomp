#!/usr/bin/env bash
# Deterministic framework-dependent publish of the dotnetapp fixture into original/.
set -euo pipefail
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
rm -rf original original.manifest.json
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
cp src/*.cs src/*.csproj "$W/"          # build in a scratch copy: keep src/ free of obj/bin
# Prefer a win-x64 framework-dependent publish (adds the dotnetapp.exe apphost). The apphost needs the
# Microsoft.NETCore.App.Host.win-x64 NuGet pack; if it cannot be restored, fall back to dll-only and say so.
if dotnet publish "$W/dotnetapp.csproj" -c Release -r win-x64 --self-contained false -p:UseAppHost=true \
     -o "$HERE/original" --nologo -v q >"$W/publish.log" 2>&1; then
  APPHOST=yes
else
  echo "dotnetapp: WARNING win-x64 apphost unavailable, publishing portable dll only" >&2; tail -5 "$W/publish.log" >&2
  rm -rf original
  dotnet publish "$W/dotnetapp.csproj" -c Release -o "$HERE/original" --nologo -v q -p:UseAppHost=false >"$W/publish.log" 2>&1 \
    || { cat "$W/publish.log"; exit 1; }
  APPHOST=no
fi
find original -type f -exec touch -d @0 {} +
python3 ../tools/make_manifest.py dotnetapp original original.manifest.json
echo "dotnetapp: apphost=$APPHOST published: $(ls original | tr '\n' ' ')"
