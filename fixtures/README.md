# Rebuild Studio acceptance fixtures

Five source-known targets for M4/M5-M8/M16. Per DR-8, `fixtures/<name>/src` is ground truth for the evaluation harness only;
adapters/reconstruction only ever receive `fixtures/<name>/original` (and `original-electron` for webapp).

| Fixture | Kind | Original (installation root) | Oracle | Expected files | Features |
|---------|------|------------------------------|--------|----------------|----------|
| [`pecli`](pecli/README.md) | native PE x64 CLI (C, mingw) | `original/` pecli.exe, README.txt, sample.dat | wine 9.0 (non-certifying) | `expected/scenarios.json` (8 scenarios) | 8 + 1 windows-only |
| [`dotnetapp`](dotnetapp/README.md) | .NET 8 console, framework-dependent | `original/` dll, exe apphost, runtimeconfig, deps | dotnet 8.0.31 on Linux | `expected/scenarios.json` (9 scenarios) | 8 + 1 windows-only |
| [`javacli`](javacli/README.md) | Java 17 console jar (ledger CLI) | `original/` javacli.jar, javacli.cmd, README.txt | java 17 (JDK 17.0.20.1, Windows host; non-certifying elsewhere) | `expected/scenarios.json` (11 scenarios) | 9 + 1 windows-only |
| [`godotgame`](godotgame/README.md) | Godot 4.3 PCK (own Python packer) | `original/game.pck` + engine-binary note | GDRE tools 2.7.0 recovery | `expected/resources.json` | 6 recoverable + 4 engine-only |
| [`webapp`](webapp/README.md) | PWA + Electron-style asar | `original/`, `original-electron/` | Playwright 1.56.1 / Chromium 141 | `expected/web_scenarios.json`, `expected/screens/` | 10 + 1 electron-only |

`manifest.json` maps fixture id -> original paths, per-file sha256, expected-file hashes and feature ids. `<fixture>/features.json` is the feature source.

## Commands
* `fixtures/build_all.sh` builds all originals, checks they reproduce `manifest.json` exactly, runs the oracle self-checks
  (pecli positive control + rejected wrong remake, dotnetapp replay, PCK verification, Playwright replay). ~20 s, no wine needed. Fails on any error.
* `fixtures/build_all.sh --regen` also re-records every `expected/` file (wine for pecli, dotnet, GDRE, Playwright) and refreshes `manifest.json`.
* `python3 tools/scenario_runner.py check --fixture <pecli|dotnetapp|javacli> --launcher "<cmd>"` compares any candidate executable with the frozen oracle
  (stdout/stderr compared after CRLF normalization; files by sha256).
* `cd webapp/harness && npm test` records, `npm run check -- --site DIR` compares a web build.

## Rules followed
Every expected value comes from actually running the original here (wine / dotnet / Chromium / GDRE). What cannot run on Linux is listed with a non-Linux
`observable_on` (`windows_only`, `godot_engine_only`, `electron_runtime_only`) and carries no expected values. Wine/Linux results are non-certifying (DR-9).
Builds are reproducible (mingw `--no-insert-timestamp`, `Deterministic` C#, sorted PCK/asar, epoch-0 mtimes).

## Pins
mingw GCC 13-win32; wine 9.0; JDK 17.0.20.1 (javacli, `javac --release 17 -g`); .NET SDK 8.0.131 / runtime 8.0.31; GDRE tools 2.7.0 (embeds Godot 4.8-dev editor); Godot PCK format 2 with engine 4.3.0;
`@electron/asar` 4.3.1; Playwright 1.56.1 (Chromium 141.0.7390.37, `/opt/pw-browsers`); Node 22.22.0; Rust cargo (wrong_remake, std only).
Committed size ~1 MB (plus gitignored `webapp/harness/node_modules`, ~21 MB, restored by `npm ci`).
