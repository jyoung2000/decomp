# CHECKPOINT

## 2026-10-06 ~12:50 UTC — all workers integrated; final validation running
- Integrated: Windows packaging/CI handoff (20 gates), UI e2e + real-controller run, Cutter plugin (20 tests), dotnetapp→Rust demo (9/9).
- Controller changes from worker findings: validation errors in the documented error shape (400), `/capabilities`, `POST /cases/{id}/modules/{mid}/briefing`, feedback requires existing case, package-data for schema/harness, build artifacts untracked, OS-aware test skips, screenshot settle wait in the web harness.
- Known open items: UI does not yet call `deliver`/`review` endpoints (documented); screenshot `pixel:exact` can differ between runs if the page is captured before settling (settle wait added; cross-host comparisons need a declared tolerance); Windows gates W1–W20 and Hermes G1–G9 unexecuted; Cutter plugin unexercised inside Cutter.

## 2026-10-06 ~12:25 UTC — demos complete, doctor verified
- `rebuildctl doctor --verify --smoke`: rizin, ilspy, gdre, jsweb **verified** (fixture regressions recorded in <data>/backend-verification.json, bound to tool versions); ghidra missing (optional).
- Demos: pecli→Rust 8/8 (MCP external client), dotnetapp→Rust 9/9 (MCP external client, worker), webapp→PWA 4/4 (deterministic), godotgame→Bevy scaffold (recovery only, honest no-parity). Records under `examples/`.
- Demo controller on port 8766 stopped. Workers still in flight: Windows packaging/CI, UI e2e + real controller, Cutter plugin.

## 2026-10-06 ~12:30 UTC — all four fixture pipelines run; native + web demos verified
- Full controller suite: 598 passed / 1 skipped. e2e set running. UI vitest 53. cargo check ok.
- Demos: `examples/pecli-rust-from-evidence` (Rust from rz-ghidra evidence via MCP: verifier 8/8, independent harness 8/8, delivered);
  web fixture pipeline 4/4 verified + independent harness exact; dotnetapp pipeline recovers C# (Rust demo via MCP in progress by worker);
  godotgame: GDRE recovery + Bevy scaffold builds (303 s), no gameplay parity claims.
- Demo controller for pecli still running on port 8766 (data: scratchpad/pecli-demo); kill via pid in controller.json before packaging runs.
- Workers in flight: Windows packaging/CI (Build/Install/Uninstall ps1, workflows, gates doc), UI e2e + real-controller spec, dotnetapp→Rust demo, Cutter plugin.

## 2026-10-06 ~12:00 UTC — vertical slice working on pecli; other fixtures running
Branch `ccr-4dbe9b92-nyzsa8`. Commits so far: bootstrap → core → fixtures/verifier → rizin/API → worker output → jobs fan-in → pipeline fixes.

### Verified on this host (Linux, non-certifying for Windows)
- `controller/tests`: core (31), inventory (6+1 skip), verifier gate (3), fixture oracle (2: original 8/8 & 9/9 pass, wrong remake 0/8 rejected), API (3), rizin (32, real rizin + rz-ghidra), managed backends (109: ILSpy 9.1.0.7988, GDRE 2.7.0, asar/js), providers/budget/router/secrets/jev (208 + 3 live skipped), MCP/CLI/installer (162), Hermes (36), e2e pipeline (2: pecli full run ≈7 s, crash/resume).
- `ui`: `npm run build` ok, vitest 53 passed; e2e against mock in progress (worker).
- `desktop/src-tauri`: `cargo check` passes on Linux (tauri 2.12.1 pinned).
- Fixtures: pecli (mingw PE, oracle via wine), dotnetapp (.NET 8), godotgame (PCK v2, GDRE recovers byte-identical), webapp (PWA + app.asar); `fixtures/build_all.sh` reproducible hashes.

### Tools (not shipped, under /opt/rebuild-tools)
rizin v0.9.1 static (sha256 9102249a…) and source build + rz-ghidra v0.9.0 (commit 999df7b8); GDRE 2.7.0 (sha256 abb4c197…); ilspycmd 9.1.0.7988 (~/.dotnet/tools); dotnet 8.0.131; mingw 13; wine 9.0; Python venv /opt/rebuild-tools/venv (rzpipe 0.6.2, lief 1.0.0, mcp 2.3.0, fastapi 0.142.2, playwright 1.58.2 in controller/harness with Chromium 1194 via explicit executablePath).

### Decisions made while integrating
- Recovery fan-in uses dynamic job dependencies (`JobStore.add_dependency`); a BLOCKED job (missing tool) cascades a visible blocker to dependents instead of spinning.
- Verifier returns `untested` for an empty baseline; `verified` requires ≥1 scenario passing all channels.
- Fixture oracles are converted (never edited) by `fixture_oracle.py`; setup files are inlined so candidate runs never read the original root.
- No-AI mode on native/managed inputs produces a buildable scaffold + bounded `ai_task_packet` evidence and marks M-IMPL blocked with the exact next action (connect a model, or use an external client via MCP `propose_candidate`). This is reported, never faked.

### Remaining
- M7: Rust/Bevy reconstruction beyond scaffold needs a model route or external client; demonstrate via MCP propose_candidate on pecli (planned next).
- M9/M15: UI e2e + real-controller run (worker), Windows packaging scripts/CI (worker).
- M13: knowledge tests + reuse demo with RaisingProvider.
- M16: web/dotnet/godot pipeline runs (in progress), then `pytest -m e2e` for all.
- M17: final report generation (`reports/final-report.{md,json}`), SUPPORT_MATRIX update, PLAN statuses.
- Windows gates: see docs/WINDOWS_RELEASE_GATES.md (worker) + docs/HERMES.md §7.
