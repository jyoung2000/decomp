# Rebuild Studio — final report

Generated 2026-10-06T12:31:34.895555+00:00 on Linux 6.18.44-fc-v70 (certifies Windows: False). Branch `ccr-4dbe9b92-nyzsa8`.

## Implementation commits

- `93e323a Record UI e2e and desktop test runs`
- `8e3b532 Support matrix rows for UI, Cutter, packaging; DR-4 note`
- `e27e3c6 Plan/checkpoint: workers integrated`
- `593c32e UI e2e/polish (worker); validation error shape; /capabilities; API doc corrections; screenshot settle wait; refreshed Godot example evidence`
- `61a9e73 Cutter plugin + client tests (worker); HTTP briefing endpoint; feedback requires an existing case`
- `03bbf80 Windows packaging/CI handoff (worker); package data for schema/harness; untrack build artifacts; OS-aware test skips`
- `52aa8a8 Checkpoint: in-progress worker output (UI e2e screens/specs, desktop shell fixes, packaging scripts)`
- `739b7bd Checkpoint`
- `44a9a9a Web and Godot example records; final report wording`
- `53cb214 dotnetapp managed-path demo (Rust from ILSpy evidence via MCP, verified 9/9); report wording: runners used, candidate author`
- `78d26a1 Scaffold candidates carry recovered original-language material and decompiled C as intermediate evidence`
- `1422b3b API doc: deliver, review, subscriptions, doctor states, error mapping`
- `68622dd Doctor: usable via smoke, verified via recorded fixture regression bound to tool versions`
- `5b7dab4 Record e2e run`
- `d13cc41 Checkpoint; cap evidence body reads`
- `6751289 Record test runs; plan statuses`
- `3eb3e01 Support matrix update; plan/preview/feedback cycle test`
- `1751a8e Final report generator and test-run recorder`
- `5165c3f Usage docs and README`
- `70b7551 pecli native demo: Rust candidate proposed through MCP from rz-ghidra evidence, verified 8/8; deliver operation`
- `46db2fc Web pipeline: single root recovery job, harness module resolution`
- `11c99fd Knowledge: signature/rewrite application and no-AI reuse demonstration`
- `e9d7a7d Update plan and checkpoint`
- `e825044 Pipeline: oracle features in ledger, manifest covers plan exports, untested verdict for empty baselines, goto templating`
- `d2fefc0 Jobs: dynamic dependencies for recovery fan-in; blocked cascade`
- `5dcfe1c Providers/budgets/JeV, MCP server, CLI, client packages, Hermes bridge, managed backends, UI and Tauri shell (worker output, integration in progress)`
- `5d8e6c1 Rizin native backend with rz-ghidra decompilation, loopback API server, fixture oracle adapter`
- `778c932 Fixtures (pecli, dotnetapp, godotgame, webapp) with frozen oracles; verifier, comparators, plan/feedback/preview stores, pipeline stages, builders, delivery and reports`
- `3f13319 Controller core: durable store, event log, job DAG/runner, path policy, case and evidence stores, adapter contract`
- `b5fc38b Bootstrap Rebuild Studio plan and checkpoint`

## Dependency pins

- Controller: pydantic>=2.13,<3, fastapi>=0.142,<1, uvicorn[standard]>=0.54,<1, websockets>=15, httpx>=0.28,<1, rzpipe==0.6.2, lief>=1.0,<2, pefile>=2024.8, mcp>=2.3,<3, pillow>=12, pyyaml>=6
- UI: {"@tauri-apps/api": "2.12.1", "react": "18.3.1", "react-dom": "18.3.1", "react-router-dom": "6.30.6", "@playwright/test": "1.56.1", "typescript": "5.9.3", "vite": "5.4.21", "vitest": "2.1.9"}
- Tauri crates: {"tauri-build": "=2.7.1", "tauri": "=2.12.1"}
- Host tools: {"rizin": ["rizin 0.9.1 @ linux-x86-64"], "wine": "wine-9.0 (Ubuntu 9.0~repack-4build3)", "dotnet": "8.0.131", "cargo": "cargo 1.97.0 (c980f4866 2026-06-30)", "node": "v22.22.0", "python": "3.13.16"}
- rizin build manifest: {"rizin": {"tag": "v0.9.1", "commit": "c3a90e9226d977f58f4e9c75f78fa6b07afe13c7", "source": "https://github.com/rizinorg/rizin", "meson_options": "-Dbuildtype=release -Denable_tests=false -Denable_rz_test=false -Duse_sys_zlib=enabled -Duse_sys_lzma=enabled -Duse_sys_libzstd=enabled -Duse_sys_openssl=enabled -Dinstall_sigdb=false", "subprojects_from_git": {"tree-sitter-0.26.9": "7f534862c3ec939c3a6ee147f7600ef5c1bf900f", "lz4-1.10.0": "ebb370ca83af193212df4dcbadcc5d87bc0de2f0", "libzip-1.11.4": "6f8a0cdd24a0dc6cce9dac4a7679da784ab124ea"}}, "rz_ghidra": {"tag": "v0.9.0", "commit": "999df7b8e7891

## Backend adapters on this host (live doctor)

| Backend | Availability | Tools |
|---|---|---|
| gdre | verified | gdre_tools verified 2.7.0 |
| ghidra | missing | ghidra missing  |
| ilspy | verified | ilspycmd verified 9.1.0.7988 |
| jsweb | verified | jsweb-native verified 1; node installed 22.22.0; asar installed 4.3.1 |
| rizin | verified | rizin verified 0.9.1 |

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
- **pecli**: native_pe_x64_cli, 9 declared features, oracle: wine (non-certifying)
- **webapp**: pwa_and_electron_asar, 11 declared features, oracle: Playwright 1.56.1 + Chromium

## Demonstrations (verifier-decided)

- **dotnetapp-rust-from-evidence** (dotnetapp → rust): full parity YES; 8/8 features verified, 0 failed, 0 untested; 42 comparisons; candidate author: controller; AI calls via app routes: none; host certifies Windows: False
- **godotgame-bevy-scaffold** (godotgame → rust_bevy): full parity NO; 0/0 features verified, 0 failed, 0 untested; 0 comparisons; candidate author: controller; AI calls via app routes: none; host certifies Windows: n/a
- **pecli-rust-from-evidence** (pecli → rust): full parity YES; 8/8 features verified, 0 failed, 0 untested; 77 comparisons; candidate author: controller; AI calls via app routes: none; host certifies Windows: False
- **webapp-pwa-port** (webapp → web): full parity NO; 4/5 features verified, 0 failed, 1 untested; 20 comparisons; candidate author: controller; AI calls via app routes: none; host certifies Windows: False

## Windows release gates (not certified here)

- Gate summary

## Hermes gates


## Limits and remaining user-dependent actions

- No interactive Windows host in this session: installer/WebView2/UI capture/Hermes desktop/DPAPI are handoffs (docs/WINDOWS_RELEASE_GATES.md).
- PE originals were executed under wine; the reports label that runner non-certifying.
- No provider API key was supplied: provider adapters are covered by mocked protocol tests only; zero paid calls were made.
- Unity IL2CPP, GameMaker, Android/JVM, Unreal profiles are detected but marked experimental/unverified (no backend run).
- Godot → Bevy produces a buildable scaffold plus recovered project; gameplay parity is untested because the original cannot run here and no scenarios were declared.
