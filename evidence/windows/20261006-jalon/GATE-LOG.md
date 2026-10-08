# Interactive Windows gate log — 2026-10-06, host "jalon"

Machine: Windows 11 Home 10.0.26200 x64, interactive desktop (owner account; admin-capable, installs were per-user and needed no elevation),
WebView2 Runtime 154.0.4258.53, two monitors (2560x1440 primary, 1920x1080 secondary). Not a clean machine: developer tools are installed.
Driven by Claude Code through Windows UI Automation (real mouse clicks / double-clicks on the real desktop), observed by screenshot.

## Run 1 — build d32eb7a (installer sha256 9a10a4fada7073e93c1db7c346ad454fc601bfc93ac23d6cfd4be210c57987e1, UNSIGNED)

| Step | Result | Evidence |
|------|--------|----------|
| Build on Windows (`Build-RebuildStudio.ps1`, PowerShell 5.1) | FAIL then fixed: `[char].Trim` (array unrolling) and stripped `"` in `python -c` under PS 5.1; after fixes UI + PyInstaller sidecar + NSIS + portable built; sidecar smoke `serve --port 0` wrote controller.json and answered /health | build logs (scratch), commit d32eb7a |
| GUI install from a folder with spaces | PASS: Welcome → Install location (default `%LOCALAPPDATA%\Rebuild Studio`) → progress → finish; no UAC prompt | screenshots during session |
| Finish page offers desktop shortcut | PASS: "Create desktop shortcut" checkbox, ticked by default; "Run Rebuild Studio" also offered | UIA tree of finish page |
| Shortcuts / app entry | PASS: `Desktop\Rebuild Studio.lnk`, `Start Menu\Programs\Rebuild Studio.lnk`, HKCU Uninstall key with DisplayIcon/InstallLocation; desktop .lnk `System.AppUserModel.ID` = `io.rebuildstudio.desktop` | reg query + Shell property read |
| Double-click desktop icon → full UI, no console | PARTIAL: UI window opened, no console/terminal/dev server; processes: rebuild-studio.exe + rebuild-controller.exe (PyInstaller onefile bootloader + child). Controller ready ≈5.3 s after spawn (cold, first run). **UI showed "Disconnected"** | screenshot; controller.log |
| Root cause | Controller answered the CORS preflight with 401 (token check ran first) and sent no `Access-Control-Allow-Origin`; the packaged webview origin `http://tauri.localhost` is cross-origin to `127.0.0.1:<port>`. Never seen before because UI e2e ran via the Vite dev server. Fixed in server.py + tests (`test_packaged_ui_origin_gets_cors`, `test_lookalike_origins_are_refused`) | |
| Normal close (window X) | PASS: all rebuild-* processes exited within 4 s; controller.json removed | tasklist |

## Run 2 — build after commit 9774d49 work tree (installer sha256 7c9c3e5e4aaedca41a82f7d6a5e96d735a069cfef92b937a702375bd9257f276, UNSIGNED)

| Step | Result | Evidence |
|------|--------|----------|
| Update over existing install (`setup.exe /P`, passive) | PASS: exit 0 in 5 s; binaries replaced; desktop shortcut kept; `%LOCALAPPDATA%\RebuildStudio` (store, cases, logs) preserved | ls before/after |
| Start-menu launch | NOT RUN: this machine uses a third-party classic Start menu; the owner was actively typing in another window, so desktop automation was stopped to avoid interfering (one mistaken keystroke burst went into another app's text box and was removed character-for-character; nothing was sent) | — |
| Pin to taskbar / relaunch from pin | NOT RUN (needs the desktop; Windows also blocks programmatic pinning by design, so this is a human step) | — |

## Run 3 — commit 80ff419 (installer sha256 b1c15bba9b17a0063eda218385e2f1cfe6a2d5adf3d1e31cb1bad929cef614c4, portable 21830bae…, UNSIGNED)

| Step | Result | Evidence |
|------|--------|----------|
| Build gate: every backend loads in the frozen controller | PASS: "sidecar doctor ok: 7 backends load in the frozen build" (Run 2's build had shipped with all backends '(failed to load)') | build log |
| Guided tool setup (no PowerShell) into `%LOCALAPPDATA%\RebuildStudio\tools` | PASS: rizin (Cutter v2.5.0 build with rz-ghidra, installed via Install-from-file), GDRE 2.7.0, .NET 8.0.31, ILSpy 9.1.0.7988, Node 22.22.0, playwright-core 1.58.2 — all hash-verified | tool_setup status |
| Installed controller doctor (--smoke) with those tools | rizin/ilspy/gdre/jsweb/triage USABLE; jvm missing (optional CFR not installed); ghidra (standalone) missing | doctor JSON |
| Real fixture through the installed controller, PATH = System32 only, no Playwright Chromium | PASS ×3: fixtures/webapp → 10/10 jobs, outcome `fully_matched` within the 1 declared (automatic) scenario, browser = system Microsoft Edge, ≈8 s each | manifest.json |
| .NET fixture through the installed controller, PATH = System32 only | BLOCKED (honest): "install Rust toolchain (rustup) to build Rust candidates" — guided setup had no Rust entry (being added) | result.json |
| Ordinary uninstall (`uninstall.exe /S`) | PASS: program dir, desktop + Start-menu shortcuts, Apps entry removed; `%LOCALAPPDATA%\RebuildStudio` (cases, tools, logs, store) and Credential Manager entries kept | ls/reg/cmdkey |
| Data-removal credential purge (`rebuild-studio.exe --remove-stored-credentials`, what the uninstaller runs when "Delete the application data" is ticked) | PASS: test credential `RebuildStudio:gate-test` removed, no window, no process left | cmdkey |
| Uninstall with the "Delete the application data" checkbox ticked (GUI) | NOT RUN (needs the uninstaller GUI) | — |
| Start-menu shortcut launch (shell-executed .lnk) | PASS: controller.json in 1.9 s; UI shows "Connected · up to date"; Tools entry present; no console window; taskbar shows one Rebuild Studio button | run3-startmenu-launch-connected.png |
| Forced termination of the shell (`taskkill /F rebuild-studio.exe`, i.e. a crash) | PASS: both controller processes gone within 2 s (Job Object kill-on-close); stale controller.json left behind | tasklist |
| Relaunch after the crash | PASS: stale controller.json replaced (new port/token), exactly one controller (PyInstaller bootloader + child), /health ok | controller.json, tasklist |
| Normal close (WM_CLOSE to the main window) | PASS: 0 processes, controller.json removed | tasklist |
| Pin to taskbar → close → relaunch from the pin | NOT RUN: Windows blocks programmatic pinning; needs a person (steps in docs/INSTALL.md) | — |

## Run 4 — commit 1d619ca+ab6494c (installer sha256 206a5a8e088cf90eab1b0ba977b32a86dae8e3b00a30a5df962dd1a0558f1032, portable b6fe6d64…, UNSIGNED)

| Step | Result | Evidence |
|------|--------|----------|
| Private Rust toolchain via guided setup (tool `rust`: pinned rustup-init 1.29.1, GNU host 1.97.0) | PASS: installed in 14 s, 849 MB, no admin, no Visual Studio | tool_setup status |
| Installed controller, PATH = System32 only: fixtures/webapp | PASS: 10/10 jobs, `fully_matched` within the declared scenario, 26 s | manifest.json |
| Installed controller, PATH = System32 only: fixtures/dotnetapp (No AI) | PASS (honest): ILSpy recovery + scaffold built with the private Rust toolchain; outcome `scaffolded`, `SCAFFOLD_NOT_IMPLEMENTED.txt` in output root and dist/; 133 s (cold cargo cache) | manifest.json, parity-report.md |
| Installed controller, PATH = System32 only: fixtures/pecli (No AI) | PASS (honest): rizin + rz-ghidra recovery, scaffold built, `scaffolded`; 36 s | manifest.json |
| Installed controller API: local Ollama connection, probe, `all_local` preset, forecast | PASS: probe 44 s (13 models, context windows from /api/show); ladders: deepseek-coder-v2:16b → qwen2.5:14b → gemma4:12b for implementation/repair, gemma4/llama3.2-vision for visual review; forecast text states local attempts and no metered cost | API responses |
| Local AI repair through the real implement loop (dev controller, same code as the build) | PASS: missing model → `model_unavailable` with reason → qwen2.5:14b repaired tests/data/tinycalc on attempt 1, verifier 3/3, $0, ~38 s; deepseek-coder-v2:16b also 3/3 | test_live_local.py report |

## IMPORTANT CORRECTION — runs 1–4 were virtualized installs

Every installer in runs 1–4 was started from processes inside the Claude desktop app (an MSIX-packaged app). Windows redirected their
`%LOCALAPPDATA%` writes into `C:\Users\jalon\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\` (same file ID seen at
both paths from inside; Explorer reported the real `C:\Users\jalon\AppData\Local\Rebuild Studio` "unavailable" —
run5-real-localappdata-has-no-install.png). Shortcuts still launched the app via link tracking, so launches/lifecycle/pinning results
are real behaviour of the app binaries, but they were NOT a genuine user install. Runs 1–4 install/uninstall/data-location rows are
therefore superseded by run 5.

## Run 5 — genuine install via Explorer (commit da31c6c build, installer sha256 dbfd651cc01f439b8a18abfaf0f583f4a781a0c0a4fef070efd3cf1c9b663383, UNSIGNED)

| Step | Result | Evidence |
|------|--------|----------|
| Remove the virtualized copy (`uninstall.exe /S`) | PASS: virtual store emptied; desktop shortcut and pin removed | — |
| Double-click the setup file in an Explorer window (installer parent = explorer.exe) | PASS: GUI pages, destination `C:\Users\jalon\AppData\Local\Rebuild Studio`, nothing written to the Claude package store | run5-real-install-folder-via-explorer.png |
| Desktop shortcut (finish page, default on) shows the app icon; double-click → real exe launched by Explorer | PASS: process path `C:\Users\jalon\AppData\Local\Rebuild Studio\rebuild-studio.exe`, UI Connected, first-run empty state points to Tools | run5-real-first-run-desktop-icon.png |
| Guided tool setup through the GUI (ILSpy + private .NET runtime) | PASS: live progress (15.0 of 31.8 MB · 47%), dependent tool queued, disabled buttons explained, both Installed | run5-real-gui-tool-install-progress.png |
| New Project with native folder pickers (output path with spaces), consent + No AI explanations | PASS | run5-real-new-project-consent-and-no-ai.png |
| Start → honest blocked state | FOUND 2 BUGS: .NET launcher sent to Rizin blocked recovery; blocker text pointed at a non-existent 'Settings → Dependencies'. Fixed in a341d70 (+ test_dotnet_apphost.py) — not yet re-verified in a rebuilt genuine install | Plan tab |
| Pin to taskbar / relaunch from pin / grouping | PASS earlier on the virtualized binaries (run 5 pre-correction, run5-launched-from-taskbar-pin.png); not repeated on the genuine install | — |
| GUI uninstall with "Delete the application data" | NOT RUN (cancelled when the virtualization was discovered) | — |

## Run 7 — genuine install, build fc6abf6 (installer sha256 e189663ea67a97be21a3bc7ef5f3b22945917f9c37d3112acf5f69abc11f632d, UNSIGNED), 2026-10-07

| Step | Result | Evidence |
|------|--------|----------|
| Update in place via setup double-clicked in Explorer (installer finished and auto-ran the app) | PASS: real `%LOCALAPPDATA%\Rebuild Studio` binaries replaced (controller sha256 matches the build), project data kept | sha256 compare |
| Cross-drive delivery (data on C:, output on D:, path with spaces) after the fix | PASS: the previously failed delivery job re-queued via the app API completed: 28 files, `manifest.json` outcome `scaffolded`, scaffold exe runs and exits 64 with "no features implemented", project status → delivered, UI "Delivered — scaffold only", no staging folder left behind | manifest, Now doing lines |
| Start-menu launch | PASS: controller ready in 3.0 s, UI connected, reopened the delivered project | tasklist |
| Pin to taskbar (jump list) → close → relaunch from the pin | PASS: pin AUMID io.rebuildstudio.desktop = process AUMID, target = genuine exe; relaunch from the pin: Explorer-parented processes, /health ok, grouped under the same button with correct name/thumbnail | run7-genuine-relaunch-from-taskbar-pin.png |
| GUI uninstall with "Delete the application data" ticked | PASS: program folder, desktop + Start-menu shortcuts, taskbar pin, Apps entry, `%LOCALAPPDATA%\RebuildStudio` and `RebuildStudio:*` Credential Manager entry removed; a sibling folder (`RebuildStudio.keep`) untouched; no processes left | ls/reg/cmdkey |
| Reinstall after data-removal uninstall | PASS (passive install) | — |
| Project that failed under the OLD build stayed 'running' with Resume disabled | KNOWN: the status settle is event-driven and only applies to failures under the new build; re-queuing the job via the API worked. New failures settle correctly (regression test) | — |

## Run 8 — .NET rebuild with local AI through the genuine install (2026-10-07/08)

Genuine install updated in place via Explorer-launched passive installer for each build; the installed controller's
sha256 was checked against the build each time (last: c1bd68f5… = commit 219dcef).

- Auto-detection (no user setup): Ollama 0.35 at 127.0.0.1:11434 found on launch, "Ollama (this PC)" connection created,
  13 models rated per task; LM Studio / llama.cpp reported not running. "Use detected local models" applied `all_local`.
- Tools installed through the app's tool setup into the genuine tools dir: private .NET runtime, ilspycmd, Rust compiler.
  (Earlier copies were in the MSIX-redirected Claude package folder, invisible to the genuine app.)
- Project "Notes app (.NET) with local AI": source fixtures/dotnetapp/original, output D:\Rebuild Studio Test Output\…,
  consent recorded, 7 user scenarios recorded by running the original isolated (Low integrity + Job Object), local only, $0.

| Run | Ladder / attempts | Result | Product bug found → fix |
| --- | --- | --- | --- |
| 1 | all_local, 3 | blocked at capture | recorded user scenarios ignored → b3de2f7 |
| 1b | all_local, 3 | 4 models × 300 s timeout, scaffold 0/7 | fixed total timeout → always stream (12cdab4) |
| 2 | all_local, 3 | recovery failed on re-run | stale recovery dir → 6fd6f5e |
| 3 | all_local, 3 | deepseek answered (208–235 s) but `std = "1"` dep, then E0425; 0/7 | built-in crates dropped (7d13b1e) |
| 4 | project override qwen2.5:14b first, 3 | qwen JSON had an unescaped quote; gemma4 spent 16k tokens thinking, no text; 0/7 | JSON-schema format + think:false (e7287e9) |
| 5 | same, 6 | attempt 1 built→syntax error; repair refused (17.5k prompt + 16k reserve > 32k) | output reserve shrinks to fit (219dcef) |
| 6 | same, 6 | all 6 attempts answered (384–543 s each, $0) and were compiled; none compiled cleanly (escape bug ×4, then E0599); 0/7 | — (model capability) |

Verdict, every run: "Scaffold only: not a working remake", Behavior verified 0 of 7 — delivered and labelled honestly; the
verifier, not the model, decided. Not achieved: a working .NET → Rust remake with the local models on this RTX 4070 12 GB.
