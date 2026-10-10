# Using Rebuild Studio

## What it does
Point it at an application/game installation folder, choose an output folder, a target (Rust, Rust + Bevy, HTML/CSS/JS, or Auto)
and an output type, optionally connect a model, press **Rebuild & Verify**. The controller inventories the folder, detects the
profile (native PE/ELF, .NET, Godot, JS/Electron…), recovers code/assets with real local tools (rizin + rz-ghidra, ILSpy, GDRE,
asar/JS extraction), builds a feature ledger, captures a baseline from the original (only when you authorise execution, or from a
frozen fixture oracle), reconstructs, builds, compares per feature, and publishes `source/ dist/ evidence/ reports/` with a manifest.
Everything the UI shows is derived from durable controller events; verdicts are written only by the verifier.

## Developer run (Linux or Windows)
```
# controller
cd controller && python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
rebuildctl doctor                      # tool availability: missing/detected/installed/usable/verified
rebuildctl serve --port 8765 --data-dir ~/.local/share/rebuild-studio   # writes <data>/controller.json {port, token, pid}

# UI (dev)
cd ui && npm ci
VITE_CONTROLLER_URL=http://127.0.0.1:8765 VITE_CONTROLLER_TOKEN=<token from controller.json> npm run dev
npm run dev:mock                       # UI against the mock controller (clearly labelled demo data)

# desktop shell (Tauri 2) — Linux check / Windows build
cd desktop/src-tauri && cargo check    # Linux validation
pwsh scripts/windows/Build-RebuildStudio.ps1   # Windows: UI + sidecar + NSIS installer + portable zip (see docs/PACKAGING.md)
```

## CLI
```
rebuildctl rebuild --source <folder> --output <folder> --language rust|rust_bevy|web|csharp|java|auto --type exe|installer|portable|web|pwa [--ai no_ai|assist_on_failure|assisted] [--execute-original] [--wait]
rebuildctl status <case_id> | jobs <case_id> | cancel <case_id> | resume <case_id>
rebuildctl evidence search <case_id> <query> | evidence get <evidence_id>
rebuildctl export-plan <case_id>       # project-plan.json + project-plan.html under <output>/reports/
rebuildctl mcp --toolset minimal|analysis|rebuild|all   # stdio MCP server for Claude Code / Codex / Gemini / Hermes
```

## Model clients (optional)
No global skill is required for the desktop app. To let an external client propose candidates:
```
python scripts/install-clients.py --client claude-code|codex|gemini|hermes|all --dry-run   # shows the config diff
python scripts/install-clients.py --client claude-code                                     # idempotent merge, backup first
python scripts/install-clients.py --client claude-code --remove
```
The client reads evidence (`search_evidence`, `get_evidence`, `get_function_briefing`), proposes files (`propose_candidate`),
builds and compares; it can never set verification verdicts. See `examples/pecli-rust-from-evidence/`.

## AI connections
Connections → add a provider (OpenAI Responses, Anthropic, Gemini, OpenRouter, local OpenAI-compatible, custom), probe it, assign
models per task (implementation, repair, naming, visual review, verification assist, knowledge) with fallbacks. A per-job budget is required
before any automatic cloud work; unknown pricing requires explicit approval. Subscription "handoff" modes only launch your own
installed CLI (`codex`, `claude`, `gemini`) with a task packet; see docs/PROVIDERS.md for what is and is not supported.

## Fixtures and regressions
```
fixtures/build_all.sh                    # rebuild originals + verify reproducible hashes
cd controller && pytest -q -m "not e2e and not live"     # unit/integration (real rizin, ILSpy, GDRE, Playwright)
cd controller && pytest -q -m e2e                        # full pipelines on fixtures
```
Hosts without Windows run PE originals under wine for behavioural baselines; every report labels that runner as non-certifying.

## Output layout
`source/` (editable target-language project with lock files) · `dist/` (exe / web build) · `evidence/` (feature ledger, inventory,
comparisons, provenance, manifests) · `reports/` (parity-report.md/json, unresolved.json, project-plan.json/html) · `manifest.json`.
