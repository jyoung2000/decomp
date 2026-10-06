# Rebuild Studio — final report

Generated 2026-10-06T18:37:15.013826+00:00 on Windows-11-10.0.26200-SP0 (certifies Windows: False). Branch `ccr-4dbe9b92-nyzsa8`.

## Implementation commits

- `c42355b Windows gate log run 3: installed-app fixtures, guided tools, uninstall/data-keep, crash/relaunch/close drills`
- `80ff419 Tests: isolate the per-user tools dir in the jsweb no-node probe test`
- `d94b546 Web capture: automatic start-page check waits for a registering service worker and skips the racy pre-load snapshot`
- `34800f6 Found via the installed controller: web projects infer how to start the original; --wait returns blockers instead of hanging`
- `1077820 Browser comparisons in the installed app: Tools-page Node + playwright-core, Edge as the browser; short isolated home`
- `44cd4fb Windows native decompilation: pin Cutter v2.5.0's rizin (same commit as rizin 0.9.1) which carries rz-ghidra`
- `40e99a8 Tests: hosted Windows runner portability (gcc writes app.exe; process-limit test uses the base interpreter)`
- `7db84c1 Scenario authoring and consent in the UI: declare CLI scenarios, record the original (with permission) into a new baseline revision`
- `5fc16f2 In-app implement/build/compare/repair loop with case budget, retries and resumable attempts; JVM pipeline routing; POSIX file-size cap off by default`
- `689e5b4 Support matrix: Java/Android recovery (CFR, jadx, pinned JRE); precise detection + honest blockers for IL2CPP, GameMaker, Unreal, Mach-O`
- `38f6daa Plan r3: state at usage-limit stop`
- `ef09bcd Plan r2 statuses; non-developer install guide (NSIS .exe, shortcuts, pinning, tools, uninstall data choice)`
- `357e9d0 Tests: portable PE sample (committed fixture, no mingw); diagnostics for Linux sandbox failures seen in CI`
- `c008034 Build: bundle every controller module explicitly; fail the build if any backend cannot load when frozen`
- `a0df280 Guided tool setup from the app: hash-verified downloads with progress, retry, cancel, offline install-from-file`
- `9774d49 Packaged UI could not reach its controller: answer CORS preflight before the token check`
- `df9cb85 Truthful outcome states: pipeline progress separate from measured behaviour; no 100% unless fully matched`
- `d663af6 Isolate untrusted runs: Job Object caps + low integrity + scrubbed env; consent before running the original`
- `a133113 Controller on native Windows: reject Windows protected roots, report zip members as stored; port POSIX-only tests`
- `d32eb7a Windows shell/installer: AUMID, desktop-shortcut-aware uninstall data removal, orphan reaping, recovery dialog; PS 5.1 build fixes; pin .NET 8.0.31 runtime`
- `ce602d7 Plan: production-readiness P-series on a real Windows host; drop stray duplicate file`
- `e33b4aa Final report; cutter test expects documented validation status`
- `93e323a Record UI e2e and desktop test runs`
- `8e3b532 Support matrix rows for UI, Cutter, packaging; DR-4 note`
- `e27e3c6 Plan/checkpoint: workers integrated`
- `593c32e UI e2e/polish (worker); validation error shape; /capabilities; API doc corrections; screenshot settle wait; refreshed Godot example evidence`
- `61a9e73 Cutter plugin + client tests (worker); HTTP briefing endpoint; feedback requires an existing case`
- `03bbf80 Windows packaging/CI handoff (worker); package data for schema/harness; untrack build artifacts; OS-aware test skips`
- `52aa8a8 Checkpoint: in-progress worker output (UI e2e screens/specs, desktop shell fixes, packaging scripts)`
- `739b7bd Checkpoint`

## Dependency pins

- Controller: pydantic>=2.13,<3, fastapi>=0.142,<1, uvicorn[standard]>=0.54,<1, websockets>=15, httpx>=0.28,<1, rzpipe==0.6.2, lief>=1.0,<2, pefile>=2024.8, mcp>=2.3,<3, pillow>=12, pyyaml>=6
- UI: {"@tauri-apps/api": "2.12.1", "react": "18.3.1", "react-dom": "18.3.1", "react-router-dom": "6.30.6", "@playwright/test": "1.56.1", "typescript": "5.9.3", "vite": "5.4.21", "vitest": "2.1.9"}
- Tauri crates: {"tauri-build": "=2.7.1", "tauri": "=2.12.1"}
- Host tools: {"rizin": ["<error [WinError 2] The system cannot find the file specified>"], "wine": "<error [WinError 2] The system cannot find the file specified>", "dotnet": "8.0.424", "cargo": "cargo 1.98.1 (797e8a9bc 2026-08-05)", "node": "v22.23.2", "python": "3.12.10"}
- rizin build manifest: n/a

## Backend adapters on this host (doctor source: live)

| Backend | Availability | Tools |
|---|---|---|
| gdre | installed | gdre_tools installed 2.7.0 |
| ghidra | missing | ghidra missing  |
| ilspy | installed | ilspycmd installed 9.1.0.7988 |
| jsweb | installed | jsweb-native installed 1; node installed 22.22.0; asar missing  |
| jvm | missing | java installed 17.0.20.1; cfr missing ; jadx missing  |
| rizin | installed | rizin installed 0.9.1 |
| triage | installed | builtin-parsers installed 1 |

## Test runs (recorded from real executions)

| Suite | Command | Passed | Failed | Skipped | When |
|---|---|---|---|---|---|
| controller unit+integration (not e2e, not live) — final | `cd controller && pytest -q tests -m 'not e2e and not live'` | 619 | 0 | 1 | 2026-10-06T12:31:34+00:00 |
| controller e2e pipelines (pecli full + crash/resume, web preview/feedback cycle) | `cd controller && pytest -q tests -m e2e` | 3 | 0 | 0 | 2026-10-06T12:15:55+00:00 |
| rebuildctl doctor --verify --smoke (fixture regressions per backend) | `cd controller && rebuildctl doctor --verify --smoke --json` | 4 | 0 | 1 | 2026-10-06T12:20:17+00:00 |
| desktop shell cargo test (Linux; Windows-only tests compiled out) | `cd desktop && sh scripts/check.sh test` | 24 | 0 | 0 | 2026-10-06T12:28:37+00:00 |
| ui playwright e2e vs mock controller | `cd ui && npm run e2e` | 18 | 0 | 4 | 2026-10-06T12:29:00+00:00 |
| ui vitest | `cd ui && npm test` | 68 | 0 | 0 | 2026-10-06T12:29:00+00:00 |
| ui playwright real-controller spec (worker run, web fixture end to end) | `cd ui && REAL_CONTROLLER=1 REAL_CONTROLLER_DATA=<data> npx playwright test real-controller` | 4 | 0 | 0 | 2026-10-06T12:40:00+00:00 |

## Fixtures

- **dotnetapp**: dotnet8_console, 9 declared features, oracle: dotnet 8 runtime on Linux (non-certifying)
- **godotgame**: godot4_pck, 10 declared features, oracle: GDRE tools 2.7.0 recovery (game itself not executable here)
- **javacli**: java17_console_jar, 10 declared features, oracle: java 17 (Temurin/Microsoft OpenJDK) on the Windows host (non-certifying for other hosts)
- **pecli**: native_pe_x64_cli, 9 declared features, oracle: wine (non-certifying)
- **webapp**: pwa_and_electron_asar, 11 declared features, oracle: Playwright 1.56.1 + Chromium

## Demonstrations (verifier-decided)

Each line separates **pipeline** facts (what was built and published) from **measured behaviour** (declared scenarios compared against the original). Scenario results cover only the scenarios declared for that case. They are not a claim of global parity: behaviour outside the declared scenarios, features with no scenario and Windows-only behaviour are not measured.

- **dotnetapp-rust-from-evidence** (dotnetapp -> rust): **Fully matched within declared coverage**. Behavior verified: 9 of 9 declared scenarios passed, 0 failed, 0 not run. Ledger features with no scenario: 0 of 8. Fixture-declared features not in the ledger: 1 (dotnetapp.apphost_exe). Recorded comparison rows: 84 across all candidates (rows are per channel per step, not scenario counts). Final candidate author: external MCP client (propose_candidate over the rebuild-mcp stdio server). App-routed AI calls: 0. Host certifies Windows: False. Recorded case status: delivered.
- **godotgame-bevy-scaffold** (godotgame -> rust_bevy): **Scaffold only: not a working remake**. Behavior verified: 0 of 0 declared scenarios passed, 0 failed, 0 not run. Ledger features with no scenario: 0 of 0. Fixture-declared features not in the ledger: 10 (godotgame.pck_recover, godotgame.engine_version_detect, godotgame.scripts_recover, godotgame.scenes_recover, godotgame.audio_asset, godotgame.project_settings, godotgame.player_movement, godotgame.save_system, godotgame.audio_player, godotgame.score_label). Recorded comparison rows: 0 across all candidates (rows are per channel per step, not scenario counts). Final candidate author: controller-authored scaffold (deterministic, no AI). App-routed AI calls: 0. Host certifies Windows: n/a. Recorded case status: running.
- **pecli-rust-from-evidence** (pecli -> rust): **Fully matched within declared coverage**. Behavior verified: 8 of 8 declared scenarios passed, 0 failed, 0 not run. Ledger features with no scenario: 0 of 8. Fixture-declared features not in the ledger: 1 (pecli.windows_native_exec). Recorded comparison rows: 154 across all candidates (rows are per channel per step, not scenario counts). Final candidate author: external MCP client (propose_candidate over the rebuild-mcp stdio server). App-routed AI calls: 0. Host certifies Windows: False. Recorded case status: delivered.
- **webapp-pwa-port** (webapp -> web): **Fully matched within declared coverage**. Behavior verified: 4 of 4 declared scenarios passed, 0 failed, 0 not run. Ledger features with no scenario: 1 of 5 (Offline/PWA behaviour). Fixture-declared features not in the ledger: 9 (webapp.delete_note, webapp.localstorage_state, webapp.hash_routes, webapp.quota_error_state, webapp.offline_reload, webapp.sw_cache_upgrade, webapp.screenshot_1280x800, webapp.electron_asar_payload, webapp.electron_runtime). Recorded comparison rows: 20 across all candidates (rows are per channel per step, not scenario counts). Final candidate author: controller-authored deterministic port of the recovered site (no AI). App-routed AI calls: 0. Host certifies Windows: False. Recorded case status: running.

### What the numbers count (nine scenarios, eight feature IDs)

These are different units and must not be mixed up:

- **Scenarios** are runnable checks recorded in the fixture oracle (`fixtures/<name>/expected/scenarios.json`). One feature can be checked by more than one scenario.
- **Feature IDs in the ledger** are the semantic features the verifier marks verified or not (`parity-report.json` > `features`). The ledger is filled from the scenarios' `feature` field, so a feature that has no scenario is not in it.
- **Feature IDs declared by the fixture** (`fixtures/<name>/features.json`) also include features that cannot be observed on this Linux host (`windows_only`); they have no scenario.

| Example | Scenarios declared / passed | Distinct feature IDs those scenarios cover | Features in the ledger | Features declared by the fixture | Declared by fixture but not in ledger | Scenarios in the full fixture oracle |
|---|---|---|---|---|---|---|
| dotnetapp-rust-from-evidence | 9 / 9 | 8 | 8 | 9 | dotnetapp.apphost_exe | 9 |
| godotgame-bevy-scaffold | 0 / 0 | 0 | 0 | 10 | godotgame.pck_recover, godotgame.engine_version_detect, godotgame.scripts_recover, godotgame.scenes_recover, godotgame.audio_asset, godotgame.project_settings, godotgame.player_movement, godotgame.save_system, godotgame.audio_player, godotgame.score_label | n/a |
| pecli-rust-from-evidence | 8 / 8 | 8 | 8 | 9 | pecli.windows_native_exec | 8 |
| webapp-pwa-port | 4 / 4 | 4 | 5 | 11 | webapp.delete_note, webapp.localstorage_state, webapp.hash_routes, webapp.quota_error_state, webapp.offline_reload, webapp.sw_cache_upgrade, webapp.screenshot_1280x800, webapp.electron_asar_payload, webapp.electron_runtime | 14 |

**.NET example, exactly:** 9 scenarios cover 8 distinct feature IDs because `dotnetapp.error_corrupt_state` is checked by 2 scenarios (`err_corrupt_json` and `err_null_json`). The fixture declares 9 feature IDs; the remaining one, `dotnetapp.apphost_exe`, is Windows-only and has no scenario, so it is neither in the ledger nor counted as verified. "Nine scenarios" and "eight features" are both correct and count different things; neither is global parity of the .NET application.

### Provenance of each candidate

The stored candidate `author` field is `model` for a proposal from an external MCP client and also for the app's own AI route, so it cannot separate them. The labels below come from the recorded MCP call log, the demo client, or the candidate origin (basis stated). The app's own AI usage is none in all demos; for the dotnetapp and pecli demos an external client (itself an AI model, outside the app's routes and budget) wrote the Rust source.

| Example | Candidate | Final | Authored by | Basis | Stored author field | App-routed AI calls |
|---|---|---|---|---|---|---|
| dotnetapp-rust-from-evidence | `cand_01a11120ef53a2efdb575f` | no | controller-authored scaffold (deterministic, no AI) | pipeline scaffold r1 that precedes the external proposal (exits 64, fails every scenario) | not recorded in this report | 0 |
| dotnetapp-rust-from-evidence | `cand_01a11124a9daabb70652b3` | yes | external MCP client (propose_candidate over the rebuild-mcp stdio server) | client-log.txt records the propose_candidate call that created it | not recorded in this report | 0 |
| godotgame-bevy-scaffold | `cand_01a11128ff9181146b55a2` | yes | controller-authored scaffold (deterministic, no AI) | candidate origin=scaffold | null (none recorded) | 0 |
| pecli-rust-from-evidence | `cand_01a1110f8ffa16e1f52b05` | no | controller-authored scaffold (deterministic, no AI) | pipeline scaffold r1 that precedes the external proposal (exits 64, fails every scenario) | not recorded in this report | 0 |
| pecli-rust-from-evidence | `cand_01a11113fa217f3ccb63d3` | yes | external MCP client (propose_candidate over the rebuild-mcp stdio server) | README.md and mcp_client_demo.py describe the proposal; no call log is kept for this example | not recorded in this report | 0 |
| webapp-pwa-port | `cand_01a1110e68a802f4e4194c` | yes | controller-authored deterministic port of the recovered site (no AI) | README.md: deterministic port; no AI usage recorded | not recorded in this report | 0 |

Recommended follow-up (not done here): record a `source` (`mcp` or `controller`) in candidate metadata so provenance does not depend on logs.

## Windows (interactive desktop)

Interactive Windows desktop: Windows 11 Home 10.0.26200 x64, WebView2 154.0.4258.53, owner's account (admin-capable; per-user install needed no elevation), two monitors. Not a clean machine (developer tools installed). Driven through Windows UI Automation and shell launches by Claude Code; evidence in evidence/windows/20261006-jalon/GATE-LOG.md.

Build under test: commit 80ff419: RebuildStudio-0.1.0-x64-setup-UNSIGNED.exe sha256 b1c15bba9b17a0063eda218385e2f1cfe6a2d5adf3d1e31cb1bad929cef614c4 (NSIS .exe, per-user), portable zip sha256 21830bae2e122f4253ebb2001a3b8d13bcfa372c14856ba7441a399b86bfaedd

| Gate | Result | Notes |
|---|---|---|
| Windows build (Build-RebuildStudio.ps1 under PowerShell 5.1) | PASS after fixes | PS 5.1 array-unrolling and quote-stripping bugs fixed; build fails if any backend cannot load in the frozen controller |
| GUI install from a path with spaces, no admin | PASS | default %LOCALAPPDATA%\Rebuild Studio; no UAC prompt |
| Desktop shortcut offered during install | PASS | finish-page checkbox 'Create desktop shortcut', ticked by default; also created by silent/passive installs |
| Start-menu entry, Apps entry, icon, AppUserModelID | PASS | shortcuts carry System.AppUserModel.ID io.rebuildstudio.desktop; the process sets the same ID |
| Double-click desktop icon → full UI, no console | PASS (after fix) | first run showed 'Disconnected' (CORS preflight 401); fixed and re-verified 'Connected' |
| Start-menu shortcut launch | PASS | controller ready in 1.9 s, UI connected, one taskbar button |
| Pin to taskbar and relaunch from pin | NOT RUN | Windows blocks programmatic pinning; needs a person |
| Normal close | PASS | all rebuild-* processes exit, controller.json removed |
| Forced termination of the shell (crash) | PASS | controller tree killed by the Job Object within 2 s |
| Relaunch after crash (stale controller.json) | PASS | new port/token, exactly one controller, /health ok |
| Update over an existing install | PASS | binaries replaced; projects, tools, keys and shortcuts kept |
| Guided dependency setup without PowerShell | PASS | rizin+rz-ghidra (Cutter v2.5.0 build), GDRE, .NET 8.0.31, ILSpy, Node, playwright-core installed and hash-verified by the app; install-from-file path exercised |
| Real fixture through the installed controller using only guided-setup components | PASS (web) / BLOCKED (Rust targets) | fixtures/webapp 3/3 fully matched within the declared scenario with system Edge, PATH=System32 only; .NET→Rust blocked: no Rust toolchain in guided setup (see unresolved) |
| Ordinary uninstall keeps data | PASS | program, shortcuts and Apps entry removed; %LOCALAPPDATA%\RebuildStudio and Credential Manager entries kept |
| Data-removal credential purge | PASS | rebuild-studio.exe --remove-stored-credentials removed RebuildStudio:* entries headlessly |
| Uninstall with 'Delete the application data' ticked (GUI) | NOT RUN | needs the uninstaller GUI |
| Full UI walk (New Project → plan → preview → feedback → scenarios → results → reopen), keyboard, DPI, narrow widths | NOT RUN | the owner was using the desktop; component tests cover the screens (UI vitest 104) |
| Standard (non-admin) user account; clean VM | NOT RUN | creating accounts/VMs was out of scope on this machine |

Measurements on this host (not a clean machine):

- Controller cold start (installed onefile sidecar): 5.3 s on the first run after install; 1.75–2.04 s warm (3 runs).
- Idle memory 20 s after a Start-menu launch: shell 31 MiB, controller 78 MiB, WebView2 345 MiB (6 processes): 461 MiB working set / 238 MiB private in total.
- Web fixture rebuild through the installed controller: 7–9 s end to end (inventory → recovery → port → build → browser comparison → delivery).
- Guided tool installs: rizin 9–12 s, GDRE 3 s, .NET runtime 2 s, ILSpy 2 s, Node + playwright-core 10 s on this connection.

## Windows CI (hosted runner)

Workflows trigger on main/PR only; runs were dispatched manually (workflow_dispatch) on branch ccr-4dbe9b92-nyzsa8. A hosted runner is not an interactive or clean machine; CI green is not a gate pass.

- windows run 37508069708 at 1077820: success (build + test on windows-latest, unsigned)
- linux run 37508065315 at 1077820: success (controller py3.12/3.13, UI, desktop cargo, PowerShell parse/dry-run, wine advisory)
- Earlier runs found and drove fixes: POSIX sandbox (.NET SIGXFSZ), hosted-runner portability (gcc app.exe, process-limit test)

## Unresolved Windows gates

- Rust toolchain for Rust remakes is not yet part of guided setup on a clean machine (in progress at report time).
- Taskbar pin + relaunch from pin, GUI uninstall with data deletion, full packaged UI walk, high-DPI/narrow-width checks: need a person at the desktop.
- Clean Windows 10/11 VM with a standard user and no developer tools; Windows Defender first-run timing on such a machine.
- Code signing (no certificate); live AI provider runs (no keys).

## Windows release gates (not certified here)

- Gate summary

## Hermes gates


## Limits and remaining user-dependent actions

- Not certified: the interactive Windows host was the owner's developer machine (dev tools installed, admin-capable account), not a clean standard-user VM. Clean-machine and standard-user gates remain open.
- Installer and portable zip are UNSIGNED (no code-signing certificate available); SmartScreen will warn.
- No AI provider key was available: the in-app implement/repair loop is verified against a scripted OpenAI-compatible server only; live Anthropic/OpenAI/Gemini/OpenRouter behaviour is UNVERIFIED and zero paid calls were made.
- Original-program isolation is damage limitation, not a security sandbox: Low integrity + Job Object limits on Windows; the program can still read most user files and the network is open unless the opt-in AppContainer mode is chosen (docs/ISOLATION.md).
- Java (.jar) recovery is supported (CFR); Android code recovery needs jadx; Unity IL2CPP, GameMaker, Unreal and Mach-O are detected with explicit blockers and no code recovery.
- Scenario results are limited to the scenarios declared for each case. Passing them is not global parity: features with no scenario, Windows-only behaviour and undiscovered scope are not measured.
- Godot → Bevy produces a buildable scaffold plus recovered project; gameplay parity is untested because no gameplay scenarios exist.
- Scenario authoring in the UI covers command-line programs; web/GUI scenarios are declared through the API or inferred (one automatic start-page check).
