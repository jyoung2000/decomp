# pecli → Rust, reconstructed from evidence through the MCP client path

This directory records the native-code demonstration run on 2026-10-06 (Linux host, non-certifying for Windows).

**What happened**
1. `rebuildctl`/Python API created a case for `fixtures/pecli/original` (stripped mingw PE x64, `pecli.exe`) with the frozen fixture oracle as baseline and AI policy **No AI**.
2. The pipeline ran: inventory → rizin 0.9.1 + rz-ghidra 0.9.0 analysis (107 functions, real Ghidra decompilation) → feature discovery (8 oracle features) → scaffold candidate r1 → build → compare (**0/8**, honest failure) → deliver. M-IMPL was left **blocked** with the next action: *open the task packet in an external client and call `propose_candidate`*.
3. An external client (this Claude Code session, acting as a model client over the real `rebuild-mcp` stdio server with `--toolset rebuild`) read `list_features`, `search_evidence`, `get_evidence` (strings, function list) and `get_function_briefing` for `0x140001d63` (main), `0x140001599` (load), `0x140001bfd` (write), `0x140001ab0` (serialize), `0x140001480` (CRC32), `0x1400014bf`/`0x1400014de` (helpers), then authored `src/main.rs` from that evidence only. The fixture's `src/` was never read by the client; original source stays outside the adapter inputs (DR-8).
4. `propose_candidate` → candidate r2 (author `model`) → `build_candidate` (cargo) → `compare_candidate` → the verifier recorded **8/8 scenarios passed** on the first attempt (exit codes, stdout/stderr normalised CRLF→LF, byte-exact store files). Feature ledger: 8 verified, full parity = YES. Candidate r2 is last known good.
5. Independent check outside the app: `fixtures/tools/scenario_runner.py check --fixture pecli --launcher <dist/pecli>` → `8 passed, 0 failed`.
6. `POST /cases/{id}/deliver` published `source/ dist/ evidence/ reports/` + `manifest.json` (24 files, every shipped file hashed). Copies of the report, comparisons and manifest are in `evidence/`.

**Caveats recorded in the report**: the original was executed under wine (host runner labelled non-certifying); the Rust binary here is an ELF built on Linux — the Windows `.exe` build of the same source is a Windows release gate (`cargo build --release` on Windows or `--target x86_64-pc-windows-gnu`).

`mcp_client_demo.py` is the small stdio client used for the calls (`tools`, `raw <tool> <json>`, `propose <files.json>`, `drive <candidate_id>`).
