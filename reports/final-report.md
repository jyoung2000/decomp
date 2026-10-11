# Rebuild Studio — final report

Generated 2026-10-11T03:42:59.017882+00:00 on Windows-11-10.0.26200-SP0 (certifies Windows: False). Branch `ccr-4dbe9b92-nyzsa8`.

## Implementation commits

- `afcb716 MCP list_functions: sort and report by code bytes (realsz), keep the address span separately`
- `739caa8 R2 evidence: recorded Claude Code session driving the RE MCP tools on pecli (open, list, decompile, rename, comment, re-decompile, xrefs)`
- `2dfe346 R7: Mach-O supported (static), Android native libraries analysed per ABI; owned fixtures`
- `ca2ff8a Windows CI: /health without a token is liveness only; PDB tests accept another MSVC toolset`
- `7d27d48 Evidence: .NET Notes app rebuilt as C# in the genuine install, 7/7 verified, no AI`
- `c3c80ba R4: rebuild in the original language first (.NET â†’ C#, JVM â†’ Java)`
- `73c0b78 Lock: UPX 5.2.1, Temurin JDK 21 and Ghidra 12.1.4 are verified pins (verify_required false)`
- `2db52f4 Build gate: check the doctor's dependency 'error' property without tripping StrictMode`
- `991ca6a R1: no-AI native analysis depth, measured against the R0 benchmark`
- `28a7773 Repair loop: detect an unchanged answer, skip the rebuild, vary, then switch rung (R5f)`
- `0daa689 R9 + R10: per-task AI control with failure rules and JeV; dependency health, one-click install, preflight`
- `9139c3a Evidence: .NET rebuild run 7 with qwen2.5-coder:14b (0/7; repairs 2-5 byte-identical)`
- `fd12aa7 R2: MCP reverse-engineering toolset, HTTP transport and copy-config UI`
- `c169b25 R0: benchmark corpus with ground truth and a scoreboard`
- `78219c9 PLAN: R-series roadmap for the reverse-engineering platform`
- `84aca40 Evidence: .NET rebuild with local AI through the genuine install (runs 1-6, all honestly scaffolded)`
- `219dcef Ollama: shrink the output reserve to fit the context instead of refusing the request`
- `e7287e9 Ollama: grammar-constrained JSON for file-map answers; thinking off unless requested`
- `7d13b1e Implement loop: drop Rust built-in crates (std/core/alloc) from a model's Cargo.toml dependencies`
- `6fd6f5e Recovery stages start from an empty output folder so a project can be started again`
- `12cdab4 Ollama: always stream so slow local generation is bounded per token, not by a fixed total timeout`
- `b3de2f7 Reuse scenarios recorded in the Scenarios tab as the rebuild's baseline; forecast counts them as verifiable`
- `53c1b57 Local AI: auto-detect local servers/models, search + download GGUF models, no silent context truncation`
- `2a2ef16 Ladder focus effect keyed on entries (hosted CI: focus moved on the wrong render); genuine-install gate run 7; plan r5; report`
- `fc6abf6 A project whose last job failed or got blocked no longer stays 'running' with Resume disabled`
- `954a8d3 Fix delivery to an output folder on another drive (WinError 17), found on a genuine Windows install`
- `ef0968a UI test: 20 s budget for the keyboard ladder test (its 5 s focus wait hit the 5 s default test timeout on Linux CI)`
- `cd02b1f Live rebuild log: plain-English job.log lines from every main stage, /cases/{id}/log, Live log tab + 'Now doing' strip`
- `fb7f5ba Correct the Windows evidence: runs 1-4 were MSIX-virtualized installs; record the genuine Explorer install (run 5); regenerate final report`
- `a8a0747 UI test: allow 5 s for the async focus move on slow CI runners`

## Dependency pins

- Controller: pydantic>=2.13,<3, fastapi>=0.142,<1, uvicorn[standard]>=0.54,<1, websockets>=15, httpx>=0.28,<1, rzpipe==0.6.2, lief>=1.0,<2, pefile>=2024.8, mcp>=2.3,<3, pillow>=12, pyyaml>=6
- UI: {"@tauri-apps/api": "2.12.1", "react": "18.3.1", "react-dom": "18.3.1", "react-router-dom": "6.30.6", "@playwright/test": "1.56.1", "typescript": "5.9.3", "vite": "5.4.21", "vitest": "2.1.9"}
- Tauri crates: {"tauri-build": "=2.7.1", "tauri": "=2.12.1"}
- Host tools: {"rizin": ["<error [WinError 2] The system cannot find the file specified>"], "wine": "<error [WinError 2] The system cannot find the file specified>", "dotnet": "8.0.424", "cargo": "cargo 1.98.1 (797e8a9bc 2026-08-05)", "node": "v22.23.2", "python": "3.12.10"}
- rizin build manifest: n/a

## Backend adapters on this host (doctor source: live)

| Backend | Availability | Tools |
|---|---|---|
| gdre | verified | gdre_tools verified 2.7.0 |
| ghidra | installed | ghidra installed 12.1.4 |
| ilspy | verified | ilspycmd verified 9.1.0.7988 |
| jsweb | installed | jsweb-native installed 1; node installed 22.22.0; asar installed 4.3.1 |
| jvm | verified | java verified 17.0.20.1; cfr verified 0.152; jadx installed 1.5.6 |
| rizin | verified | rizin verified 0.9.1 |
| triage | verified | builtin-parsers verified 1 |

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
| windows-11 controller (not e2e/live) | `pytest -m 'not e2e and not live' (REBUILD_STUDIO_TOOLS=app tools dir)` | 802 | 0 | 9 | 2026-10-07T00:15:49+00:00 |
| windows-11 desktop cargo test | `cargo test (desktop/src-tauri)` | 28 | 0 | 0 | 2026-10-07T00:15:49+00:00 |
| windows-11 ui vitest | `npx vitest run` | 114 | 0 | 0 | 2026-10-07T02:42:05+00:00 |
| windows-11 controller e2e | `pytest -m e2e (real rizin+rz-ghidra, ILSpy, GDRE, Edge/Chromium, cargo)` | 20 | 0 | 1 | 2026-10-07T02:42:05+00:00 |

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

Interactive Windows desktop: Windows 11 Home 10.0.26200 x64, WebView2 154.0.4258.53, owner's account (admin-capable; per-user install needed no elevation), two monitors. Not a clean machine (developer tools installed). Driven through Windows UI Automation and shell launches by Claude Code; evidence in evidence/windows/20261006-jalon/GATE-LOG.md. IMPORTANT: installs in runs 1–4 were launched from inside the Claude desktop app (MSIX) and were file-system virtualized into its private AppData; run 5 is the genuine install launched from Explorer.

Build under test: commit fc6abf6: RebuildStudio-0.1.0-x64-setup-UNSIGNED.exe sha256 e189663ea67a97be21a3bc7ef5f3b22945917f9c37d3112acf5f69abc11f632d (NSIS .exe, per-user); portable 88dacb69…

| Gate | Result | Notes |
|---|---|---|
| Genuine install: setup double-clicked in Explorer, GUI pages, path with spaces, no admin | PASS | real %LOCALAPPDATA%\Rebuild Studio verified in Explorer |
| Desktop shortcut offered (default on); app icon; double-click → full UI, no console | PASS | Explorer launched the real exe; UI Connected |
| Start-menu entry / AppUserModelID | PASS (genuine install) | Start-menu launch: ready in 3.0 s, connected |
| Pin to taskbar → close → relaunch from pin; icon and grouping | PASS (genuine install) | jump-list pin carries AUMID io.rebuildstudio.desktop = process AUMID; relaunch from pin healthy and grouped |
| Guided tool setup through the GUI | PASS | ILSpy + .NET runtime with live progress; explained disabled buttons |
| New Project with native folder pickers; consent and No AI explanations | PASS | output path with spaces |
| Real fixture through the genuine install | PASS (honest scaffold) | .NET fixture: ILSpy recovery, scaffold built with the private Rust toolchain, delivered to a D: output folder (cross-drive fix verified); outcome scaffolded; no scenarios declared so nothing claimed verified |
| Lifecycle: normal close, crash kill, relaunch with stale controller.json | PASS (virtualized install, same binaries); normal close also PASS on the genuine install | Job Object kills the controller tree |
| Update in place; ordinary uninstall keeps data; credential purge | PASS (genuine install) | update via Explorer-launched setup kept projects; see also data-removal gate |
| GUI uninstall with 'Delete the application data' | PASS (genuine install) | program, shortcuts, pin, Apps entry, %LOCALAPPDATA%\RebuildStudio and RebuildStudio:* credentials removed; unrelated sibling folder untouched |
| Installed controller completes fixtures with only app-installed tools (CLI, PATH=System32) | PASS (virtualized install) | web fully_matched within declared scenario; .NET and native PE honestly scaffolded |
| AI ladder UI vs real controller + local Ollama (dev build) | PASS | Playwright 3/3; qwen2.5:14b repaired tinycalc, verifier 3/3 |
| Standard user account; clean VM; signed build | NOT RUN | not available on this machine |
| Live rebuild log in the installed app | PASS | Live log tab streamed plain-English stage lines during a real rebuild; 'Now doing' strip on Overview |
| Bugs found only on the genuine install | FIXED + re-verified | .NET apphost blocked recovery; stale blocker text; cross-drive delivery (WinError 17); failed project stuck 'running' — all fixed with regression tests; cross-drive and apphost fixes re-verified on the rebuilt genuine install |

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

- Clean Windows 10/11 VM with a standard (non-admin) user and no developer tools; Windows Defender first-run timing there.
- Code signing (no certificate): installer and portable zip are UNSIGNED; SmartScreen will warn.
- Live cloud AI providers (no API keys): verified only against a scripted provider and local Ollama.
- Projects that failed under a build older than fc6abf6 keep status 'running' until re-queued (status settle is event-driven).

## Analysis quality without AI (benchmark, reports/benchmark.md)

Generated 2026-10-10T17:08:41Z. Function recall/precision are against ground truth from our own builds; rows not run keep their reason.

| Row | Status | Function recall | Precision | Named recall | Real-decompiler coverage | Packed |
|---|---|---|---|---|---|---|
| c_msvc_x64_o2 | ok | 91.6 % | 100.0 % | 47.4 % | 86.4 % |  |
| c_msvc_x86_o2 | ok | 95.8 % | 100.0 % | 53.7 % | 95.5 % |  |
| cpp_msvc_x64_o2 | ok | 84.2 % | 100.0 % | 32.1 % | 79.0 % |  |
| dotnet_plain | ok | n/a | n/a | n/a | 100.0 % |  |
| dotnet_renamed | ok | n/a | n/a | n/a | 100.0 % |  |
| go_apk_arm64 | ok | 99.3 % | 97.2 % | 91.8 % | 100.0 % |  |
| go_elf_x64 | ok | 99.2 % | 99.1 % | 93.7 % | 100.0 % |  |
| go_macho_arm64 | ok | 99.2 % | 95.5 % | 91.5 % | 100.0 % |  |
| go_macho_fat | ok | n/a | n/a | n/a | n/a |  |
| go_macho_x64 | ok | 99.2 % | 97.1 % | 91.7 % | 100.0 % |  |
| go_pe_x64 | ok | 99.4 % | 99.0 % | 94.5 % | 100.0 % |  |
| rust_msvc_x64 | ok | 84.5 % | 89.8 % | 25.1 % | 100.0 % |  |
| upx_c_msvc_x64 | ok | 91.6 % | 100.0 % | 47.4 % | 86.4 % | packed (UPX, confidence 0.95) |
| c_mingw_x64_o2s | not run: mingw-w64 (x86_64-w64-mingw32-gcc) is not installed on this host | n/a | n/a | n/a | n/a |  |
| c_mingw_x86_o2s | not run: mingw-w64 (i686-w64-mingw32-gcc) is not installed on this host | n/a | n/a | n/a | n/a |  |
| c_gcc_elf_stripped | not run: no gcc/clang/zig for an ELF C build on this host (the stripped-ELF row is go_elf_ | n/a | n/a | n/a | n/a |  |
| unity_il2cpp | not run: needs the Unity editor with the IL2CPP module to build a project we own; not inst | n/a | n/a | n/a | n/a |  |
| gamemaker | not run: no free GameMaker runner/CLI build is available on this host | n/a | n/a | n/a | n/a |  |
| dotnet_confuserex | not run as ConfuserEx itself (.NET Framework GUI/CLI, unmaintained); renaming is reproduce | n/a | n/a | n/a | n/a |  |
| pecli | ok | n/a | n/a | n/a | n/a |  |
| dotnetapp | ok | n/a | n/a | n/a | 100.0 % |  |
| javacli | not scored by R0 metrics: JVM jar (CFR path); parity is measured by its scenario oracle | n/a | n/a | n/a | n/a |  |
| godotgame | not scored by R0 metrics: Godot PCK (GDRE path); recovery is checked by expected/resources | n/a | n/a | n/a | n/a |  |
| webapp | not scored by R0 metrics: web/asar (no native code); parity is measured by its Playwright  | n/a | n/a | n/a | n/a |  |

## Platform milestones (PLAN.md R-series)

| ID | Milestone | Status |
|---|---|---|
| R0 | Benchmark corpus + scoreboard | done 2026-10-10 (CI wiring still todo) |
| R1 | No-AI native analysis depth | done 2026-10-10 except mingw/Go signature packs (no mingw on the host; Go is already named from pclntab), Swift demangling (rizin 0.9.1 has no Swift demangler) and CI wiring of the Ghidra test |
| R2 | MCP as an RE tool (Ghidra/Cutter-class) | done 2026-10-10. Recorded client session: `evidence/mcp/claude-code-re-session.jsonl` — Claude Code 2.1.287 (haiku, headless, temporary --mcp-config, toolset re) opened pecli.exe, listed functions by size, decompiled, renamed, commented, re-decompiled and saw both, followed xrefs; 9 turns, $0.12. Finding for R1: on mingw-built pecli the largest function is reported as 23,718 bytes but decompiles t |
| R3 | Cloud AI: keys in, verified live | todo (blocked on keys) |
| R4 | Rebuild in the original language first | done 2026-10-10 except `cargo fix`/rustc suggested edits and `dotnet format` as repair steps (the error-driven rules below cover the compile errors seen on the corpus; formatting changes no behaviour) and a .NET Framework/Android path (C# target needs a .NET Core/5+ assembly; Android needs the Android SDK, refused with a plain reason) |
| R5 | AI-assisted RE quality (local and cloud) | todo |
| R6 | RE workbench in the app | todo |
| R7 | More input kinds, each with a fixture we own | done 2026-10-10 for Mach-O and Android native libraries; Unity IL2CPP, GameMaker and Unreal stay detected-only (no fixture we own can be built on this host) |
| R8 | Release gates | todo |
| R9 | Granular AI control per task + JeV | done 2026-10-10 except the recorded live JeV call (opt-in `tests/test_jev_live.py`, `REBUILD_LIVE_JEV=1`; not run by the implementing agent). Contract: docs/AI_LADDER.md section 9; tests `test_ai_control.py`, `test_jev.py`, `AiControl.test.tsx` |
| R10 | No missing dependencies: checked, installed, running | done 2026-10-10; installer gate executed in a local build (2026-10-10, after a StrictMode fix, commit 2db52f4) and in Windows CI |

## Runs through the installed app (evidence/windows/20261006-jalon/GATE-LOG.md)

- Run 1 — build d32eb7a (installer sha256 9a10a4fada7073e93c1db7c346ad454fc601bfc93ac23d6cfd4be210c57987e1, UNSIGNED)
- Run 2 — build after commit 9774d49 work tree (installer sha256 7c9c3e5e4aaedca41a82f7d6a5e96d735a069cfef92b937a702375bd9257f276, UNSIGNED)
- Run 3 — commit 80ff419 (installer sha256 b1c15bba9b17a0063eda218385e2f1cfe6a2d5adf3d1e31cb1bad929cef614c4, portable 21830bae…, UNSIGNED)
- Run 4 — commit 1d619ca+ab6494c (installer sha256 206a5a8e088cf90eab1b0ba977b32a86dae8e3b00a30a5df962dd1a0558f1032, portable b6fe6d64…, UNSIGNED)
- Run 5 — genuine install via Explorer (commit da31c6c build, installer sha256 dbfd651cc01f439b8a18abfaf0f583f4a781a0c0a4fef070efd3cf1c9b663383, UNSIGNED)
- Run 7 — genuine install, build fc6abf6 (installer sha256 e189663ea67a97be21a3bc7ef5f3b22945917f9c37d3112acf5f69abc11f632d, UNSIGNED), 2026-10-07
- Run 8 — .NET rebuild with local AI through the genuine install (2026-10-07/08)
- Run 9 — .NET Notes app rebuilt as C# through the genuine install (2026-10-10, R4)

## Windows release gates (not certified here)

- Gate summary

## Hermes gates


## Limits and remaining user-dependent actions

- Not certified: the interactive Windows host was the owner's developer machine (dev tools installed, admin-capable account), not a clean standard-user VM. Clean-machine and standard-user gates remain open.
- Installer and portable zip are UNSIGNED (no code-signing certificate available); SmartScreen will warn.
- No cloud AI provider key was available: live Anthropic/OpenAI/Gemini/OpenRouter behaviour is UNVERIFIED and zero paid calls were made (R3 waits for the owner's key). Local AI (Ollama) is verified end to end; on an RTX 4070 12 GB, no local model produced a compiling Rust port of the .NET fixture (runs 1-7), while the C# target rebuilt it with no AI (run 9).
- Original-program isolation is damage limitation, not a security sandbox: Low integrity + Job Object limits on Windows; the program can still read most user files and the network is open unless the opt-in AppContainer mode is chosen (docs/ISOLATION.md).
- Java (.jar) recovery is supported (CFR); Android code recovery needs jadx (native lib/<abi>/*.so libraries are analysed by Rizin per ABI); Mach-O is analysed by Rizin (fat files per slice; static only, Mach-O cannot run on Windows); Unity IL2CPP, GameMaker and Unreal are detected with explicit blockers and no code recovery.
- Scenario results are limited to the scenarios declared for each case. Passing them is not global parity: features with no scenario, Windows-only behaviour and undiscovered scope are not measured.
- Godot → Bevy produces a buildable scaffold plus recovered project; gameplay parity is untested because no gameplay scenarios exist.
- Scenario authoring in the UI covers command-line programs; web/GUI scenarios are declared through the API or inferred (one automatic start-page check).
