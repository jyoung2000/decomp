#!/usr/bin/env bash
# Replays all scenarios against the shipped original/ and expects an exact match with expected/scenarios.json.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$(command -v python3 || command -v python)}"
JARPATH="$HERE/original/javacli.jar"; command -v cygpath >/dev/null && JARPATH="$(cygpath -m "$JARPATH")"
"$PY" "$HERE/../tools/scenario_runner.py" check --fixture "$HERE" --launcher "java -jar $JARPATH"
