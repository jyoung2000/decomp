#!/usr/bin/env bash
# Deterministic build of the javacli fixture into original/ (javacli.jar + javacli.cmd + README.txt).
# Needs a JDK 17 (javac) on PATH or JAVA_HOME and Python. The jar is assembled by tools/make_jar.py (sorted entries, fixed
# timestamps, STORED) so the bytes depend only on javac's class output for the pinned JDK (see README).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
PY="${PYTHON:-$(command -v python3 || command -v python)}"
if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" -o -x "$JAVA_HOME/bin/javac.exe" ]; then JAVAC="$JAVA_HOME/bin/javac"; else JAVAC=javac; fi
rm -rf original original.manifest.json
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/classes" original
# -g: full debug info, the default of Maven/Gradle builds (a decompiler can then name parameters and locals). Built with -g:none, CFR 0.152 emits
# `string`/`n` names that no longer match record components and the recovered Entry.java does not compile. --release 17 pins class version 61.
"$JAVAC" --release 17 -g -encoding UTF-8 -Xlint:none -d "$W/classes" $(find src -name '*.java' | sort)
"$PY" ../tools/make_jar.py "$W/classes" original/javacli.jar --main-class dev.rebuild.ledger.Main --title javacli --version 1.0
printf '@echo off\r\njava -jar "%%~dp0javacli.jar" %%*\r\n' > original/javacli.cmd
printf 'javacli 1.0 - tiny ledger CLI\r\nUsage: javacli ledger.txt add NAME AMOUNT ^| remove NAME ^| list ^| total ^| stats\r\nRequires Java 17 or newer.\r\n' > original/README.txt
find original -type f -exec touch -d @0 {} +
"$PY" ../tools/make_manifest.py javacli original original.manifest.json
echo "javacli: published: $(ls original | tr '\n' ' ')"
