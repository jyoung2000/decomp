# dotnetapp fixture (.NET 8 console app)

Stdin-driven "Notes App": main menu -> Notes / Settings -> back. State is JSON saved to the path in `argv[0]`
(after every change and on exit). `src/` holds `Program.cs`, `Menus.cs`, `AppState.cs`, `dotnetapp.csproj`
(ground truth, never given to adapters). `original/` holds only the framework-dependent publish output.

## Build
`./build.sh` copies `src/` to a scratch dir (so `src/` never gets obj/bin) and runs
`dotnet publish -c Release -r win-x64 --self-contained false -p:UseAppHost=true -o original` (SDK 8.0.131,
`Deterministic`, `ContinuousIntegrationBuild`, `DebugType=none`, `InvariantGlobalization`). Result:
`dotnetapp.dll`, `dotnetapp.exe` (win-x64 apphost, 151552 bytes), `dotnetapp.runtimeconfig.json`, `dotnetapp.deps.json`.
Files get epoch-0 mtimes; two builds give identical hashes (dll `e646609f...a4cd`).
The apphost needs the `Microsoft.NETCore.App.Host.win-x64` NuGet pack (downloaded on first build, then cached);
if it cannot be restored the script warns and falls back to a dll-only publish, which makes `manifest.json` verification fail loudly.

## Behaviour
Menus: main `1` Notes, `2` Settings, `0`/`q` Quit; Notes `1` add (prompts `Text:`), `2` list, `3` delete (prompts a number), `0` back;
Settings `1` user name, `2` toggle theme light/dark, `0` back. EOF on stdin quits cleanly. Exit codes: 0 ok, 2 usage (argc != 1),
4 corrupt state (invalid JSON or `null` document) with a two-line friendly message on stderr. State JSON is indented, LF line endings,
no timestamps (byte-reproducible).

## Features (stable ids)
`dotnetapp.menu_navigation`, `dotnetapp.menu_notes`, `dotnetapp.menu_settings`, `dotnetapp.menu_eof`, `dotnetapp.state_save`,
`dotnetapp.state_load`, `dotnetapp.error_corrupt_state`, `dotnetapp.error_usage` (Linux-observable via `dotnet original/dotnetapp.dll`)
and `dotnetapp.apphost_exe` (`windows_only`: the PE apphost cannot run on this host).

## Oracle
`python3 ../tools/scenario_runner.py generate --fixture .` runs **9 scenarios (11 steps)** against
`dotnet original/dotnetapp.dll` (runtime 8.0.31, Linux; non-certifying for Windows): quit/save defaults, notes flow, settings flow,
navigation with unknown/empty inputs, load + delete across two runs, stdin EOF, corrupt JSON (exit 4), `null` JSON (exit 4),
usage (exit 2, two steps). Each records exit code, stdout (always LF), stderr (`Console.Error` newline is LF on Linux, CRLF on Windows:
compare `stderr_normalized`) and final state file sha256 + text. `harness/selfcheck.sh` replays everything and must match 9/9.
