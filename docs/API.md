# Controller API contract (UI ⇄ controller)

Transport: loopback HTTP (`http://127.0.0.1:<port>`) + WebSocket `/ws`. The port and a per-launch token are written to
`<data_dir>/controller.json` (`{"port":..,"token":..,"pid":..}`) by the sidecar; the Tauri shell reads it and injects both into the UI
via `window.__REBUILD_STUDIO__ = {baseUrl, token}` (dev: Vite proxy with `VITE_CONTROLLER_URL`/`VITE_CONTROLLER_TOKEN`).
Every request carries `Authorization: Bearer <token>`. Requests whose `Origin` is not the app origin / `http://localhost:*` / `tauri://localhost` are refused (403).
All JSON. Timestamps ISO-8601 UTC. Errors: `{"error": {"code": str, "message": str, "affected": str?, "next_action": str?}}`. Status mapping: 401 auth, 403 origin/permission, 404 unknown id, 400 validation/path policy, 402 BudgetExhausted, 409 ApprovalRequired (unknown pricing), 424 NoRoute/AllCandidatesFailed, 503 optional service unavailable.

## Events (WebSocket `/ws?token=..&since=<seq>`)
Server sends `{"seq", "ts", "case_id", "job_id", "kind", "payload"}` objects, one per message, strictly increasing `seq` for durable events. Idle `controller.heartbeat` frames (payload `{idle:true}`) repeat the last `seq` and are liveness only; clients must not treat them as duplicates of real events.
On connect, the server replays events with `seq > since` then streams live. Clients MUST dedupe on `seq`.
`controller.heartbeat` is emitted at least every `lease_timeout_seconds` (default 30 s; `GET /health` returns the value). If no
event (any kind) arrives within 2× that interval the UI MUST mark state stale/unknown.
Kinds: `case.created|case.status`, `job.created|started|progress|log|retry|unblocked|blocked|completed|failed|cancelled|cancel_requested|resumed|needs_retest|lease_expired`,
`evidence.added|invalidated`, `plan.revised|plan.item`, `feature.updated`, `candidate.created|built|failed`, `comparison.recorded`,
`preview.published|opened|stopped`, `feedback.created|updated`, `verification.completed|invalidated`, `feature.stale`, `budget.updated`, `ai.call`, `knowledge.updated`, `controller.heartbeat`. (`plan.revised` carries only `{revision, reason, item_count}`; clients refetch the plan.)

## REST
- `GET /health` → `{ok, version, pid, heartbeat_seconds, latest_seq, started_at}`
- `GET /events?since=N&case_id=` → `[event]` (same shape as WS; bounded 5000)
- `GET /doctor?smoke=0|1` → backend availability report (`missing|detected|installed|usable|verified` per tool)
- `GET /capabilities` → `{output_combinations:[{target_language, output_type, state: supported|unsupported, reason}], profiles_with_backends, implementation:{ai_connected, summary, route}}`
- `POST /implementation/forecast` `{target_language, output_type, ai_policy, launch_profile, profile?}` → plain-language answer, before a case exists, to "can an implementation be produced?": `{state: scaffold_only|ai_ready|ai_blocked|deterministic_port|unsupported, can_produce_implementation, will_use_ai, verifiable, summary, details[], blockers[], max_attempts, budget_usd, route}`. The same `implementation_forecast` is on `POST /cases`, `GET /cases/{id}`, `GET /cases/{id}/plan` and `POST /cases/{id}/start`. AI policy keys: `mode` (no_ai|assist_on_failure|assisted), `budget_usd`, `max_attempts` (default 3, max 10; total model attempts), `max_output_tokens` (cap per call; also the bound that allows an unpriced model), `approve_unknown_pricing`, `max_retries`, `retry_backoff_s`. With a usable route the `implement_loop` job runs interpret -> write Rust -> cargo build -> verify -> repair; every attempt is `ai_attempt`/`ai_response` evidence.
- `GET /cases` / `POST /cases` body `{name, source_root, output_root, target_language: rust|rust_bevy|web|auto, output_type: exe|installer|portable|web|pwa, ai_policy:{mode: no_ai|assist_on_failure|assisted, budget_usd?, max_repairs?, approve_unknown_pricing?}, launch_profile, settings?:{decompile_limit?}}`
  - `launch_profile` = `{execute_original: bool, kind: cli|web, launch: {type: exe|dotnet|command|web, path?|command?|root?, entry?}, scenarios: [{id (required), feature_id?, title?, critical?, steps?: [{args, stdin?}] (cli), actions?: [{type: click|fill|press|goto|wait|wait_for|offline|online|reload|eval|wait_sw, ...}] (web), text_selectors?, channels?, normalize?, ignore_files?, setup_files?}], tolerance?: {screenshot_max_diff_fraction?, screenshot_channel_delta?}, baseline_file?}`
  - validation errors → 400 `{"error": {code: "validation", message, affected, next_action}}`
- `GET /cases/{id}` → case + counts (`jobs` by state, features by status, candidates, previews, open feedback)
- `POST /cases/{id}/start` → `{job_ids}`; `POST /cases/{id}/pause`; `POST /cases/{id}/resume`; `POST /cases/{id}/cancel`
- `POST /cases/{id}/deliver` body `{candidate_id?}` → deliver job (default: last known good, else newest built candidate)
- `POST /features/{id}/review` body `{review: accepted|rejected|null}` — user acceptance, separate from machine verification
- `GET /cases/{id}/jobs` → `[job]` with `progress` (raw counts + denominators, never synthesized %), `blocker`, `attempt`, `heartbeat_at`
- `POST /jobs/{id}/cancel` / `POST /jobs/{id}/resume`
- `GET /cases/{id}/modules`, `GET /cases/{id}/evidence?kind=&module_id=`, `GET /evidence/{id}?max_bytes=`, `GET /cases/{id}/evidence/search?q=`
- `GET /cases/{id}/features` → feature ledger rows (`impl_status`, `verify_status`, `user_review`, `critical`, `verify_candidate`)
- `GET /cases/{id}/plan` → `{revision, items:[item], unknown_scope:[...], progress:{jobs:{<state>:n}, groups:{discovery|recovery|implementation|build|verification: {done, total|null, unit, failed?, running?}}, features:{...}, scope_known, eta}, eta: {seconds, uncertainty_seconds, unknown_jobs, updated_at, label}|null}`; item ids are `<case_id>:<stable id>`; job `progress` fields are raw measured counts (e.g. `files_scanned`, `functions_decompiled`/`functions_total`, `scenarios_done`/`scenarios_total`)
- `GET /cases/{id}/plan/revisions`; `POST /cases/{id}/plan/prioritize` body `{item_id}`; `POST /cases/{id}/plan/change` body `{item_id, request, reason}` → affected items/tests
- `GET /cases/{id}/plan/export` → writes `project-plan.json` + `project-plan.html` into `<output_root>/reports/` and returns paths
- `GET /cases/{id}/candidates`; `GET /candidates/{id}` (manifest, build hash, verification, last_known_good)
- `GET /cases/{id}/comparisons?candidate_id=` → comparison rows with `verdict`, `rule`, `tolerance`, hashes, artifacts
- `GET /cases/{id}/previews`; `POST /previews/{id}/open` → `{kind: browser|native, url?|command?, instance_id}`; `POST /previews/{id}/stop`
- `GET /cases/{id}/feedback`; `POST /cases/{id}/feedback` body `{target_kind, target_id, candidate_id?, classification, priority, comment, expected?, actual?, attachments?:[{name, bytes_b64}]}` → persisted before 200
- `POST /feedback/{id}/triage` body `{status, note, create_work: bool}`; `POST /feedback/{id}/reopen`; `GET /feedback/{id}`
- `GET /connections` / `POST /connections` `{provider, label, endpoint?, auth_mode, api_key?, models?}`; `POST /connections/{id}/probe`; `DELETE /connections/{id}`
- `GET /routes` / `PUT /routes/{task}` `{primary_connection, primary_model, fallbacks:[{connection, model}]}`
- `GET /budgets` → `{budgets, quotas}`; `GET /ai/calls?case_id=`; `GET /subscriptions` → handoff modes with `supported`, `access_method`, `limits`, `checked_on`, `cli_available`
- `GET /doctor?smoke=0|1` states: `missing|detected|installed|usable|verified` (`usable` = smoke op passed now; `verified` = `rebuildctl doctor --verify` fixture regression recorded for these exact tool versions)
- `GET /knowledge`; `GET /knowledge/{id}`; `POST /knowledge/{id}/validate`; `POST /knowledge/{id}/rollback`
- `GET /settings` / `PUT /settings` (dependency manager, storage, concurrency, capture, network/output policies)
- `GET /hermes/status`; `POST /hermes/pair` `{profile_path?}`; `POST /hermes/register_mcp` `{dry_run}`
- `POST /fs/pick-folder` is NOT an HTTP endpoint: native pickers are Tauri commands (`pick_folder`). In browser dev mode the UI shows a text input.

## User-declared scenarios (command-line programs)

Implemented in `controller/rebuild_controller/api/scenario_routes.py` (+ `user_scenarios.py`). Errors use the standard `{error:{code,message,affected,next_action}}` body.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/cases/{id}/scenarios` | `{scenarios[], counts, consent}`; each scenario has `status`: `no_baseline`, `changed` (edited after recording), `not_run`, `passed`, `failed` |
| POST | `/cases/{id}/scenarios` | create `{title, feature_id?, steps:[{args:[..], stdin}], compare:{exit_code,output,files}, normalize:{line_endings,trailing_spaces,trim,ignore_timestamps}, timeout?}` |
| GET / PUT / DELETE | `/cases/{id}/scenarios/{sid}` | read / replace / delete (a frozen baseline is never edited) |
| POST | `/cases/{id}/scenarios/record` | `{scenario_ids?, program?}`: runs the ORIGINAL through the isolated runner and freezes a NEW baseline revision that carries earlier scenarios over. 409 `original_execution_not_permitted` ("Original execution needs your permission: ...") without recorded consent |

`{work}` in an argument is replaced by the run's private scratch folder; the folder path is masked as `{work}` in recorded and compared output. Recording a new revision marks earlier verification results stale.

## Live log

Stages write plain-English lines with `ctx.log(text, level="info"|"warn"|"error", detail=None, key=None)`. Each becomes a persisted `job.log` event
`{job_id, stage, milestone, plan_item_id, level, text, detail, at, message}` (`message` repeats `text` for older clients). Text is capped at 400 chars,
`detail` (for example the last few stderr lines of a failed tool) at 1500; both go through the providers' `redact()` and prompts are never accepted.
Rate limit: at most 5 info lines per second per job (extra progress lines coalesce to the latest per `key`, released on the next window or when the job
ends); warnings and errors have their own 5/s budget so problems are never starved.

| Method | Path | Notes |
|---|---|---|
| GET | `/cases/{id}/log?since=&limit=&level=&stage=` | `{entries:[{seq, at, kind:"log"\|"job"\|"ai", level, text, detail, job_id, stage, milestone, plan_item_id, (provider, model, outcome)}], latest_seq, limit}` oldest first. `since=0` returns the newest `limit` (default 300, max 2000); `since>0` returns entries after that seq. `level=warn` keeps warnings and errors, `level=error` errors only. Merges `job.log`, `ai.activity` and job state changes (`job.started/completed/failed/blocked/cancelled/retry`) rendered as text. |

The UI's Live log tab loads this once, then appends live events and dedupes by `seq`.

## MCP model interface (`rebuild-mcp`, and `/mcp` on the running controller)

Two transports serve the same tools (`controller/rebuild_controller/mcp/server.py`):

| Transport | How | Auth | When |
|---|---|---|---|
| stdio | the client starts `rebuild-mcp --toolset <set>` (installed at `<install>\runtime\Scripts\rebuild-mcp.exe`) | local process | permanent client setup; works without the app running |
| streamable HTTP | `POST/GET/DELETE http://127.0.0.1:<port>/mcp` on the running controller (stateless, JSON responses) | the controller's per-launch bearer token (`Authorization: Bearer <token>`), Origin allow-list, Host header must be `127.0.0.1`/`localhost`/`[::1]` | an agent should see the same analysis sessions as the open app; token and port change on every launch |

`GET /mcp/config?client=claude-code|codex|gemini|hermes|generic&transport=stdio|http&toolset=all|re|...` returns
`{client, transport, format: json|toml|yaml, where, text, notes[], command?, http_available}`: a ready-to-paste config
snippet (Claude Code also gets a `claude mcp add ...` command). The app's Settings page shows it with a **Copy MCP config** button.
Nothing is written to the client's files (use `scripts/install-clients.py` for that).

Toolsets: `minimal` ⊂ `analysis` ⊂ `rebuild` ⊂ `all`; `re` (reverse engineering, below) is its own set and is also part of `all`.
Rules kept for every tool: typed arguments only (no raw rizin/shell command anywhere), unknown arguments rejected, results bounded
by `max_context_bytes`, program-derived text wrapped as untrusted.

### Reverse-engineering sessions (`re` toolset)

- `open_binary` takes an absolute file path (ad hoc; a hidden case with `settings.kind = "re_session"` is created per file path so
  evidence is still stored; hidden from `GET /cases` unless `?include_re=1` and from `list_cases`) or an existing `case_id` +
  `module_id`. It returns `session_id`; reopening the same file or module returns the same session while it is open.
  Sessions are listed in `<data_dir>/re_sessions.json`; `close_session` stops the rizin process and keeps everything else.
- Every read tool is a typed backend operation (`RizinBackend.op_*`: functions, function, decompile, disasm, disassemble, xrefs,
  call_graph, strings, imports, exports, symbols, sections, entrypoints, search_bytes) and stores evidence (`native.*`) keyed by
  module sha256 + rizin version + analysis settings + annotation revision. The R6 app workbench uses the same operations
  (`StudioServices.re`, `rebuild_controller/re_workbench.py`).
- Annotations (`rename` function/global/local, `set_type` prototype/local, `add_comment`, `apply_struct`) are validated against
  strict grammars (identifiers, C types, prototypes, declarations without `#`, comments or quotes), applied to the live rizin
  session and verified by reading the result back, then stored as a new `re.annotations` evidence revision holding the full state
  (the newest revision is current; older ones are history, `get_annotations(history=N)`). They are replayed after every
  (re-)analysis, so a crashed or idle-reaped rizin process, a closed session and a controller restart all keep them; cached
  analysis evidence is invalidated by the annotation revision. Comments are never sent to rizin: they are overlaid onto
  disassembly (`user_comment`) and the top of decompiler output. Model-made annotations are labelled `model-proposed`.
- `patch_bytes(confirm=true)` copies the module to `<data_dir>/cases/<case>/re/<module>/patched/<name>` on first use and writes
  only that copy (the original's sha256 is re-checked after every patch); it returns the copy path/sha256 and the original and
  previous bytes and records `re.patch` evidence. Analysis keeps using the original; `open_binary(path=<copy>)` analyses the patch.
- `decompile(decompiler="ghidra")` uses Ghidra headless when `GHIDRA_INSTALL_DIR` points at an installation (annotations are not
  applied there); otherwise it fails with `unavailable`.

Error codes in `error.code`: `rejected` (bad input), `not_found`, `closed` (session closed), `invalid_target` (address/name/value
refused by the backend), `refused`, `timeout`, `unavailable`, `backend_error`, `controller_unavailable`, `internal_error`.

### MCP tool reference

Regenerate with `rebuild-mcp --list-tools --markdown` (a test fails when this block drifts from the code).

<!-- BEGIN generated: rebuild-mcp --list-tools --markdown -->

| Tool | Toolsets | Kind | Parameters (`?` = optional) | What it does |
|---|---|---|---|---|
| `doctor` | minimal, analysis, rebuild, re, all | read | `smoke`? (boolean, default false) | Report which backend tools (rizin, ilspy, gdre, node, ...) are missing/detected/installed/usable/verified. smoke=true runs a tiny real operation per tool (slower). |
| `list_cases` | minimal, analysis, rebuild, all | read | `limit`? (integer, >=1, <=100, default 25) | List existing cases (id, name, status, target, roots). |
| `create_case` | minimal, analysis, rebuild, all | write | `name` (string, len>=1, len<=120)<br>`source_root` (string)<br>`output_root` (string)<br>`target_language`? (rust\|rust_bevy\|web\|auto, default "auto")<br>`output_type`? (exe\|installer\|portable\|web\|pwa, default "exe")<br>`ai_policy`? (object)<br>`launch_profile`? (object) | Create a rebuild case for a program folder. Does not start work; call start_rebuild next. Only folders the user named may be used. launch_profile.execute_original allows running the original program and is refused unless the server was started with --allow-execute-original. |
| `start_rebuild` | minimal, analysis, rebuild, all | write | `case_id` (string) | Schedule the full analysis -> rebuild pipeline for a case. Returns job ids; poll with job_status. |
| `job_status` | minimal, analysis, rebuild, all | read | `job_id`? (string)<br>`case_id`? (string)<br>`limit`? (integer, >=1, <=200, default 50) | Status of one job (job_id) or a summary of all jobs of a case (case_id). Progress is raw counts, never a synthesized percentage. Give exactly one of job_id/case_id. |
| `cancel` | minimal, analysis, rebuild, all | write | `job_id`? (string)<br>`case_id`? (string) | Request cancellation of a job or of every job of a case (exactly one of job_id/case_id). |
| `resume` | minimal, analysis, rebuild, all | write | `job_id`? (string)<br>`case_id`? (string) | Resume failed/cancelled jobs of a case, or one job (exactly one of job_id/case_id). Work resumes from durable state. |
| `inventory` | analysis, rebuild, all | read | `case_id` (string)<br>`module_limit`? (integer, >=0, <=200, default 50) | Overview of a case: modules by format, evidence counts by kind (stale counted separately), first modules. |
| `list_modules` | analysis, rebuild, all | read | `case_id` (string)<br>`limit`? (integer, >=1, <=200, default 50)<br>`offset`? (integer, >=0, <=1000000, default 0)<br>`format`? (string) | List the modules (executables, assemblies, packs, bundles) of a case, optionally filtered by format. |
| `analyze_module` | analysis, rebuild, all | read | `case_id` (string)<br>`module_id` (string)<br>`limit`? (integer, >=1, <=500, default 100) | What is known about one module: metadata plus an index of its evidence (ids, kinds, titles). Read-only; use get_evidence for bodies. |
| `list_features` | analysis, rebuild, all | read | `case_id` (string)<br>`limit`? (integer, >=1, <=500, default 100)<br>`offset`? (integer, >=0, <=1000000, default 0)<br>`verify_status`? (untested\|verified\|partial\|failed\|stale) | Feature ledger of a case (impl_status, verify_status, review). Verification status is written by the verifier only; you cannot change it. |
| `get_function_briefing` | analysis, rebuild, all | read | `case_id` (string)<br>`module_id` (string)<br>`address`? (string)<br>`name`? (string) | Bounded briefing for one function (disassembly/decompilation summary, xrefs, strings, callers). Give exactly one of address (hex) or name. |
| `search_evidence` | analysis, rebuild, all | read | `case_id` (string)<br>`query` (string, len>=1, len<=200)<br>`kinds`? (list)<br>`limit`? (integer, >=1, <=100, default 25) | Lexical search over a case's evidence titles/metadata/small bodies. Returns ids, never full bodies. |
| `get_evidence` | analysis, rebuild, all | read | `evidence_id` (string)<br>`case_id`? (string)<br>`max_bytes`? (integer, >=1024, <=1048576) | Read one evidence item by id (bounded; truncated=true when cut). Pass case_id to make sure the evidence belongs to the case you are working on. Cite evidence ids in anything you propose. |
| `capture_original` | analysis, rebuild, all | write | `case_id` (string)<br>`scenario_id`? (string) | Schedule a capture of the original program's behaviour (a job). Refused unless the case allows running the original. |
| `propose_candidate` | rebuild, all | write | `case_id` (string)<br>`files` (object)<br>`note`? (string, len<=2000, default "")<br>`evidence_ids`? (list)<br>`base_candidate`? (string) | Propose source files as a NEW staged candidate (never touches the original or the trusted baseline). Destinations are relative text-file paths. List the evidence ids your proposal is based on. |
| `build_candidate` | rebuild, all | write | `case_id` (string)<br>`candidate_id` (string) | Schedule a build of a candidate (a job). Poll with job_status. |
| `compare_candidate` | rebuild, all | write | `case_id` (string)<br>`candidate_id` (string)<br>`feature_ids`? (list) | Schedule the verifier's comparison of a built candidate against the original (a job). You cannot set verdicts; the verifier records them. |
| `propose_knowledge` | all | write | `kind` (signature\|type_lib\|parser\|recipe\|rewrite\|template\|replay\|fixture)<br>`name` (string)<br>`body` (object)<br>`acceptance`? (object)<br>`constraints`? (object)<br>`evidence_ids`? (list)<br>`confidence`? (number, >=0, <=1, default 0.5) | Propose a reusable knowledge item. body/acceptance are JSON objects whose shape depends on kind (signature: {pattern, symbol, arch} + acceptance {positives, negatives}; rewrite: {match, replace}; parser: {struct, magic}; recipe/replay: {actions}; see the reference). Inert until the controller validates it in isolation and promotes it. |
| `validate_knowledge` | all | write | `knowledge_id` (string) | Run the controller's isolated validation for a proposed knowledge item. Promotion is not available to models. |
| `open_binary` | re, all | write | `path`? (string)<br>`case_id`? (string)<br>`module_id`? (string) | Open an analysis session on a binary: either an absolute file path (ad-hoc; no case needed, evidence and annotations are still stored and persist when the same file is opened again) or an existing case module (case_id + module_id). Returns session_id for every other re tool. Analysis (aaa) runs lazily on the first tool that needs it. |
| `list_sessions` | re, all | read | `include_closed`? (boolean, default false) | List open analysis sessions (session_id, case/module, file name, sha256). |
| `close_session` | re, all | write | `session_id` (string) | Close an analysis session and stop its rizin process. Evidence and annotations are kept. |
| `list_functions` | re, all | read | `session_id` (string)<br>`contains`? (string, len>=1, len<=120)<br>`sort`? (addr\|size\|name, default "addr")<br>`offset`? (integer, >=0, <=10000000, default 0)<br>`limit`? (integer, >=1, <=500, default 100) | Analysed functions (address, name, size, blocks, cc, signature). Filter by name substring; sort by addr\|size\|name; paged. |
| `get_function` | re, all | read | `session_id` (string)<br>`address`? (string)<br>`name`? (string) | One function: signature, calling convention, size, basic blocks, variables (args/locals with types) and its annotations. Give exactly one of address (any address inside it) or name. |
| `decompile` | re, all | read | `session_id` (string)<br>`address`? (string)<br>`name`? (string)<br>`decompiler`? (rizin\|ghidra, default "rizin") | Decompile one function. decompiler=rizin uses rz-ghidra (pdg) when loaded, otherwise rizin pseudo-code (labelled is_real_decompiler=false); decompiler=ghidra uses Ghidra headless when installed. Annotations (renames, types, structs, comments) are applied to rizin output. |
| `disassemble` | re, all | read | `session_id` (string)<br>`address`? (string)<br>`name`? (string)<br>`count`? (integer, >=1, <=2000)<br>`length`? (integer, >=1, <=65536) | Disassemble a whole function (address or name, no count/length), or linearly from an address: count instructions (<= 2000) or length bytes (<= 65536). User comments appear as user_comment. |
| `xrefs_to` | re, all | read | `session_id` (string)<br>`address`? (string)<br>`name`? (string)<br>`limit`? (integer, >=1, <=1000, default 200) | References TO an address or symbol (who calls/reads/writes it): from, type (CALL/CODE/DATA/STRING), containing function. |
| `xrefs_from` | re, all | read | `session_id` (string)<br>`address`? (string)<br>`name`? (string)<br>`limit`? (integer, >=1, <=1000, default 200) | References FROM an address (the instruction or data at it) to other addresses. |
| `call_graph` | re, all | read | `session_id` (string)<br>`address`? (string)<br>`name`? (string)<br>`direction`? (callees\|callers\|both, default "callees")<br>`depth`? (integer, >=1, <=5, default 2)<br>`max_nodes`? (integer, >=1, <=500, default 200) | Bounded call graph from one function (breadth-first): callees, callers or both; depth 1..5; at most max_nodes nodes (node_limit_hit=true when cut). |
| `strings` | re, all | read | `session_id` (string)<br>`contains`? (string, len>=1, len<=120)<br>`regex`? (string, len>=1, len<=200)<br>`min_length`? (integer, >=1, <=1000, default 4)<br>`section`? (string)<br>`offset`? (integer, >=0, <=10000000, default 0)<br>`limit`? (integer, >=1, <=500, default 100) | Strings in the binary (izz), filtered by substring (contains) or bounded regex (no nested quantifiers or backreferences), minimum length and section; paged. |
| `imports` | re, all | read | `session_id` (string)<br>`contains`? (string, len>=1, len<=120)<br>`offset`? (integer, >=0, <=10000000, default 0)<br>`limit`? (integer, >=1, <=1000, default 200) | Imported functions/symbols (library, name, PLT/IAT address); name filter; paged. |
| `exports` | re, all | read | `session_id` (string)<br>`contains`? (string, len>=1, len<=120)<br>`offset`? (integer, >=0, <=10000000, default 0)<br>`limit`? (integer, >=1, <=1000, default 200) | Exported symbols; name filter; paged. |
| `symbols` | re, all | read | `session_id` (string)<br>`contains`? (string, len>=1, len<=120)<br>`offset`? (integer, >=0, <=10000000, default 0)<br>`limit`? (integer, >=1, <=1000, default 200) | Symbol table (functions, objects, imports); name filter; paged. |
| `sections` | re, all | read | `session_id` (string) | Sections and segments (name, virtual/physical address and size, permissions). |
| `entry_points` | re, all | read | `session_id` (string) | Program entry points (address, type). |
| `search_bytes` | re, all | read | `session_id` (string)<br>`hex`? (string)<br>`string_regex`? (string, len>=1, len<=200)<br>`limit`? (integer, >=1, <=1000, default 100) | Search the binary: hex = byte pattern with ?? wildcards (e.g. '48 8b ?? 24'), or string_regex = bounded regex over extracted strings. Give exactly one. |
| `get_annotations` | re, all | read | `session_id` (string)<br>`history`? (integer, >=0, <=200, default 0) | Current annotations of the session's module (renames, prototypes, local types, comments, declared types) and optionally the last N changes. Annotations are proposals, not analysis facts. |
| `rename` | re, all | write | `session_id` (string)<br>`kind` (function\|global\|local)<br>`new_name` (string)<br>`address`? (string)<br>`name`? (string)<br>`variable`? (string) | Rename a function (address or name), a global (address; creates/renames a flag) or a local variable (function address/name + variable = its current name). Persisted as an evidence revision and re-applied after re-analysis; visible in decompile output. |
| `set_type` | re, all | write | `session_id` (string)<br>`kind` (function\|local)<br>`address`? (string)<br>`name`? (string)<br>`prototype`? (string)<br>`variable`? (string)<br>`type`? (string) | kind=function: apply a C prototype to the function at address/name (the name in the prototype becomes the function's name), e.g. 'int parse_args(int argc, char **argv)'. kind=local: set a variable's C type ('uint32_t', 'char *', 'struct point *'; declare structs first with apply_struct). Persisted; re-applied on re-analysis. |
| `add_comment` | re, all | write | `session_id` (string)<br>`address` (string)<br>`text` (string, len<=1000) | Attach a one-line comment (<= 1000 chars) to an address; empty text removes it. Shown in disassemble (user_comment) and at the top of decompile output. Persisted. |
| `apply_struct` | re, all | write | `session_id` (string)<br>`declaration` (string, len>=1, len<=16384) | Declare C types for the module from declaration text: struct/union/enum/typedef only, no '#' preprocessor lines, comments or quotes, <= 16 KiB. Afterwards usable in set_type. Persisted. |
| `patch_bytes` | re, all | write | `session_id` (string)<br>`address` (string)<br>`data` (string)<br>`confirm`? (boolean, default false) | Write bytes at an address into a COPY of the binary inside the session's work folder (the user's original file is never written). confirm must be true. Returns the copy's path and sha256 and the original/previous bytes; analysis keeps using the original (open the copy with open_binary to analyse it). At most 4096 bytes per call. |
| `admin_diagnostics` | --diagnostic only | read | - | Server/controller diagnostics for troubleshooting the integration (versions, loaded tools, limits, data directory, controller status). Enabled only with --diagnostic. Reveals no secrets. |

Every result is an envelope `{operation_id, tool, ok, evidence_revision, truncated, data | error}`; program-derived strings are wrapped as `{"untrusted": true, "text": ...}`; unknown arguments are rejected.

<!-- END generated: rebuild-mcp --list-tools --markdown -->
