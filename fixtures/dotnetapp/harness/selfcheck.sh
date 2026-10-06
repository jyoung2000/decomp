#!/usr/bin/env bash
# Replays all scenarios against the shipped original/ and expects an exact match with expected/scenarios.json
# (proves the oracle is stable/deterministic). A faulty candidate would be rejected by the same command.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
python3 "$HERE/../tools/scenario_runner.py" check --fixture "$HERE" --launcher "dotnet $HERE/original/dotnetapp.dll"
