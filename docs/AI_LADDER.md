# AI model ladder, local AI and plan visibility — contract

Status: contract for the P13 work (controller + UI). Extends the existing router (`providers/router.py`), connection store
(`providers/connections.py`, tasks `implementation, repair, naming, visual_review, verification_assist, knowledge` since R9 - see
section 9; `interpretation` is accepted as an alias of `implementation`) and routes API
(`GET /routes`, `PUT /routes/{task}`). Nothing here replaces them; field names below are additive.

## 1. Failure taxonomy (router attempt `outcome`)

| outcome | meaning | spend | next step |
|---|---|---|---|
| `ok` | answered | settled | — |
| `rate_limit` | 429; `retry_after_s` when sent | released | wait (bounded) and re-send, then next candidate |
| `usage_limit` | plan/quota period exhausted (e.g. daily limit) | released | next candidate; connection state `limited` until `reset_at` if known |
| `credits_exhausted` | prepaid credits / free tier used up (HTTP 402, `insufficient_quota`, OpenRouter credits) | released | next candidate; connection state `no_credits`; never auto-switch to a paid model unless it fits the budget and has known price (or approval) |
| `auth_failed` | 401/403 | released | next candidate; connection state `auth_failed` |
| `model_unavailable` | model deprecated / not found / not pulled (404, `model_not_found`, Ollama "model ... not found") | released | next candidate; route entry flagged `model_unavailable` |
| `capability_unsupported` | the model cannot take this input (images, tools, context window) — known before the call when possible | none | next candidate (skipped pre-call when capability or context is known) |
| `unreachable` / `timeout_not_sent` | network/connect failure, nothing sent | released | bounded re-send, then next candidate |
| `unavailable` | 5xx | released | optional re-send, then next candidate |
| `timeout` / `ambiguous` | request may have been processed | **assumed spent** | never re-sent to the same route automatically; fallback only if `fallback_on_ambiguous`; recorded as uncertain |
| `approval_required` | unknown price and no approval / no token cap | none | skipped; plan shows "needs approval" |
| `budget_exhausted` | reservation would exceed the case budget | none | skipped |
| `policy_skipped` | excluded by project policy (`no_ai`, `local_only`, `cloud_only`) | none | skipped |

Every attempt record carries `reason` (one plain-English sentence), `position` in the ladder, `locality` (`local`/`cloud`),
`config_revision` and, for the winner, `took_over_from` (the candidates that failed before it with their reasons).

## 2. Ladder API

- `GET /ai/ladder` → `{config_revision, tasks: {<task>: {entries: [Entry], rationale: "user"|"preset:<name>"|"auto"}}}`
  `Entry = {position, connection_id, connection_label, provider, model, locality, availability: {state, probed_at, detail},
  capabilities: {vision, tools, context_window}, price: {known, input_per_mtok, output_per_mtok, source}, free: bool}`
- `PUT /ai/ladder/{task}` `{entries: [{connection_id, model}]}` → bumps `config_revision` (stored snapshot).
- `GET /ai/models?q=&task=` → searchable catalog: union of probed/discovered models of all connections plus manual IDs already
  in use: `{connection_id, provider, model, locality, capabilities, price, availability}`; manual model IDs remain allowed in PUT.
- `POST /ai/ladder/preset` `{preset: "local_first"|"cloud_first"|"all_local"|"all_cloud"|"no_ai", apply: bool}` → the proposed
  ladders (preview when `apply=false`); deterministic choice from available connections, coder/vision-capable models preferred
  per task; returns `warnings` (e.g. "no local model supports images; visual review has no route").

## 3. Project policy (`case.ai_policy`, additive)

`{mode: "no_ai"|"inherit"|"custom"|<existing modes>, ladder_overrides: {<task>: [{connection_id, model}]}, locality: "any"|"local_only"|"cloud_only",
budget_usd, approve_unknown_pricing, max_attempts, max_output_tokens}`. `no_ai` → the router refuses **before** any adapter is
created (no network). Changing policy or the global ladder while a case is paused applies to work started afterwards; each AI
attempt records the `config_revision` and policy hash it used. `GET/PUT /cases/{id}/ai-policy`.

## 4. Plan visibility

Plan items and jobs that may use AI carry `ai = {task, primary: {provider, model, locality}, fallbacks: [...], rationale,
expected_cost: {min_usd, max_usd, known} | {unknown_price: true}, budget_usd, runs_without_ai: bool, without_ai: "<what happens>"}`.
Work items are labelled by origin: `deterministic`, `model_proposed`, `verifier_decided`.

## 5. Activity feed

`GET /cases/{id}/ai/activity` and live events `ai.activity`: `{at, kind, text, plan_item_id, job_id, candidate_id, evidence_ids,
task, provider, model, locality, outcome, tokens_in, tokens_out, cost_usd, cost_known, fallback_reason, config_revision, origin}`.
`text` is plain English, e.g. "Interpreting module pecli.exe with local model qwen2.5:14b", "qwen2.5:14b is not available
(model not found); trying deepseek-coder-v2:16b", "deepseek-coder-v2:16b proposed 3 files (candidate r2)", "Build passed",
"Verifier: 6 of 8 declared scenarios passed", "Next: repair attempt 3 of 3". Never contains keys or raw prompts (prompt sha256 only).

## 6. Local models

`provider: "local"` (OpenAI-compatible; Ollama `http://127.0.0.1:11434/v1`, LM Studio `http://127.0.0.1:1234/v1`). Discovery uses
`/v1/models`; for Ollama, capabilities and context window come from `/api/show` when reachable. Local calls cost $0 and need no
budget. Display: "runs on this PC". Capabilities/context are shown when known; nothing claims a small local model can handle a whole
large project.

## 7. Controller implementation notes (as built; all additive to sections 1-6)

No field above was renamed. Extra fields the controller returns:

- **Attempt records** (`CallResult.attempts`, `AllCandidatesFailed.attempts`, implement-loop `call.router_attempts`):
  `{connection_id, connection_label, provider, model, outcome, reason, position, locality, config_revision, policy_hash, ...}`;
  failed sends add `error, retry, assumed_spent, cost_usd, cost_known`; the winner adds `took_over_from: [{position, connection_id,
  provider, model, outcome, reason}]` (one per ladder position, its final outcome). Extra outcome labels besides section 1:
  `invalid_request`, `unusable` (handoff-only / deleted connection), `no_adapter`, `internal_error`. `ai_calls` rows carry a JSON
  `detail` column with `reason, position, locality, config_revision, policy_hash, took_over_from, prompt_sha256`.
  `AllCandidatesFailed.recovery` / `NoRoute.recovery` hold the precise recovery action ("To continue: add credits to X; pull the model
  ... Then resume."); the implement loop puts it into the job result `blocker` and the M-IMPL / M-FIX plan blockers.
- **State**: connection state `no_credits` (credits exhausted); a (connection, model) flag lives on the connection's model entry as
  `availability: {state: "model_unavailable", at, detail}` (cleared by the next success or a discovery that lists the model). Local
  Ollama discovery adds per model `capabilities: {completion, vision, tools, source}`, `context_window`, `meta.ollama: {family,
  parameter_size, quantization_level}` and `limits.server = "ollama"`, `limits.server_version`.
- **Free -> paid**: after a free model (known price 0) reports `credits_exhausted`, a paid fallback is used only with a known price
  (or `approve_unknown_pricing`) AND a budget reservation that fits; otherwise `approval_required` / `budget_exhausted` with the reason.
  The activity line for such a fallback says "... - a paid model, up to $X from the budget".
- **Ladder API**: `GET /ai/ladder` Entry also has `runs_on: "this PC"|"cloud"`; `availability.state` is the model flag if set, else the
  connection state (`missing` when the connection was deleted). `PUT /ai/ladder/{task}` body `{entries, allow_unlisted=true}`;
  `entries: []` clears the task; response `{config_revision, task, entries, rationale}`; 400 `{error:{code:"ladder"}}` on an unknown
  task/connection. `GET /ai/ladder/revisions` -> `{config_revision, revisions:[{revision, created_at, reason}]}`;
  `GET /ai/ladder/revisions?revision=N` -> `{revision, created_at, reason, tasks:{<task>:{entries:[{connection_id, model}], rationale}}}`.
  `PUT /routes/{task}` and connection deletion also bump `config_revision`. `GET /ai/models` rows also carry `connection_label, source,
  free`; with `task=` they add `suitable, suitability_note` and are sorted best-first. `POST /ai/ladder/preset` accepts optional
  `tasks: [...]` and returns `{preset, applied, config_revision, tasks:{<task>:{entries:[Entry], rationale:"preset:<name>"}}, warnings}`.
  Presets skip auth-failed connections, flagged models and embedding-only models, prefer coder models (interpretation/repair), verified
  vision (visual_review) and larger parameter counts; at most 4 entries per task.
- **Project policy**: `GET/PUT /cases/{id}/ai-policy` -> `{case_id, policy, policy_hash, config_revision, effective:{<task>:{entries:
  [Entry + policy_skipped: null|"<reason>"], rationale, source: "global"|"project"|"policy"}}}`. PUT merges the given keys into the
  stored policy (400 on unknown mode/locality/task/connection or negative numbers). AI is on for modes `assisted`,
  `assist_on_failure`, `inherit`, `custom`; `ladder_overrides[task]` replaces the global ladder for that task unless mode is `inherit`.
  Changing `budget_usd` also updates the case budget's limit. Optional `request_timeout_s` (per model call, useful for slow local
  models). `policy_hash` = first 16 hex of sha256 over `mode, ladder_overrides, locality, budget_usd, approve_unknown_pricing,
  max_attempts, max_output_tokens`.
- **Plan**: `ai` is set on `M-IMPL` (task `interpretation`) and `M-FIX` (task `repair`, or the interpretation ladder with a `note` when no
  repair ladder exists). Extra keys: `enabled, max_attempts, skipped:[{connection_id, model, reason}]`; `primary`/`fallbacks` entries are
  `{provider, model, locality, connection_id, connection_label, price_known, free}`; `expected_cost` adds `basis`. Every plan item has
  `origin`. The plan response adds `ai_jobs: [{job_id, stage, title, state, ai}]` for implement-loop jobs.
- **Activity**: `GET /cases/{id}/ai/activity?since=<seq>&limit=` returns the `ai.activity` event payloads plus `seq`; extra keys
  `kind` (`start|retry|fallback|answer|stopped|refused|proposal|build|verify|next`), `position, policy_hash, prompt_sha256,
  took_over_from, call_id`.

## 8. Local AI on this PC (detection, model downloads, Ollama context)

- **Detection** (`providers/local_ai.py`): probes ONLY loopback well-known endpoints, in parallel with short timeouts: Ollama
  `127.0.0.1:11434` (`/api/version`, `/api/tags`, `/api/show`), LM Studio `127.0.0.1:1234` (`/v1/models`, `/api/v0/models`),
  llama.cpp `127.0.0.1:8080` (`/v1/models`, `/props`). Runs on controller start (background thread; `REBUILD_NO_LOCAL_DETECT=1`
  disables), on `POST /ai/local/detect`, when the Connections page opens and at most every 60 s while it is visible.
  For each server found one `provider=local` connection "<Name> (this PC)" is created or reused (matched by loopback port, never
  duplicated, never renamed) and re-discovered; `limits.detected`, and for Ollama `limits.server="ollama"`, `num_ctx_cap`. A server
  that disappears keeps its connection with state `unreachable`.
- `GET /ai/local[?max_age=s]` / `POST /ai/local/detect` -> `{detected_at, num_ctx_cap, servers:[{kind, name, label, endpoint, found,
  version, probe_ms, install_page, models_folder?, connection_id, connection_state, models:[{id, server, size_bytes, family,
  parameter_size, parameter_b, quantization, context_window, effective_context, capabilities:{completion, vision, tools, thinking,
  embedding?, source}, tasks:{<task>:{ok, note}}, quick_only, excluded, suitable, summary}]}], suitable_models,
  ladder:{state:"empty"|"preset"|"user"}, recommend_use, recommended_preset:"all_local"|"local_first", advice}`.
  Rules: embedding-only models are excluded; interpretation/repair need code ability (coder model, or a general model >= 7B that is
  not an image-description model) and >= 16k context; visual_review needs vision; < 4B parameters = "quick tasks only".
- `POST /ai/local/use {apply, preset?}` -> the preset result (section 7) plus `replaces_user_ladder`. Detection never changes a
  ladder; only this explicit call (previewed first in the UI) does.
- **Ollama context (correctness fix)**: Ollama's OpenAI-compatible `/v1/chat/completions` cannot set the context and silently cuts
  long prompts to the server default (measured here, Ollama 0.35 / qwen2.5:3b: a 28,947-token prompt became 8,194 tokens, wrong
  answer, HTTP 200). Connections with `limits.server="ollama"` therefore use the native `/api/chat` adapter
  (`providers/ollama_chat.py`): `options.num_ctx` = request estimate (chars/2, power-of-two bucket >= 8192) bounded by
  min(model context, `num_ctx_cap` default 32768, settable via `PUT /ai/local/settings {num_ctx_cap}`), and `truncate:false` so an
  oversize prompt is refused (HTTP 400) instead of cut; a server-reported exact size is retried once with a bigger `num_ctx`; a
  request that cannot fit raises `ContextWindowExceeded` before sending. `ai_calls.detail` records `effective_context`, `prompt_tokens`.
- **Models** (`local_models.py`): `GET /ai/local/search?q=` (HF `/api/models?search=&filter=gguf&sort=downloads`),
  `GET /ai/local/files?repo=` (HF model info + `tree/<commit>`: per-file quant, size, LFS sha256, RAM hint, license, gated),
  `POST /ai/local/downloads {repo, path, dest_dir?, accept_license, register?}` -> job; `GET /ai/local/jobs[/id]`,
  `POST /ai/local/jobs/{id}/cancel`; `GET /ai/local/models`, `POST /ai/local/models/{id}/register`,
  `DELETE /ai/local/models/{id}?unregister=1`; `POST /ai/local/ollama/pull {name}`; `GET|PUT /ai/local/settings {models_dir,
  num_ctx_cap}`; `PUT /ai/local/hf-token {token|null}` (credential store; never returned or logged).
  Downloads: https only, huggingface.co then redirects only to `*.huggingface.co` / `*.hf.co`; token only on the first hop; `.part`
  + `Range` resume after an interruption; cancel deletes the partial; sha256 checked against the LFS oid before the file is renamed
  into place (mismatch deletes it); free space and writability checked first; non-permissive or gated licenses need
  `accept_license`; gated repos without a token fail with `gated_needs_token`. Registration: `POST /api/blobs/sha256:<d>` +
  `POST /api/create {model:"rs-<repo>-<quant>", files:{<file>:"sha256:<d>"}}`, then re-detection. Without Ollama the file is kept
  with status `needs_server` and the official install pages.

## 9. Granular AI control per task + JeV (R9)

All additive; nothing in sections 1-8 was renamed except the task id below (the old id is still accepted).

- **Tasks** (`connections.TASKS`): `implementation` (writes the program / candidate; was `interpretation`), `repair`, `naming`
  (function / variable names and comments for annotations), `visual_review`, `verification_assist`, `knowledge`. `interpretation`
  is a legacy alias accepted by every entry point (`PUT /ai/ladder/interpretation`, routes, policies, `AIClient.call`,
  `POST /ai/route/test`). **Migration** (idempotent, on controller start, `ConnectionStore.migrate_tasks`): a stored
  `task_routes` row `interpretation` becomes `implementation` (if both exist the new one wins and the old row is dropped), its rung
  rules move with it, and a new `config_revision` is stored with reason "task 'interpretation' renamed to 'implementation' ..."
  keeping the old rationale; every case `ai_policy.ladder_overrides.interpretation` is renamed once (meta key `ai_tasks_r9`).
  `normalize_policy` also maps the old key when reading. Activity lines for `implementation` read "Writing the implementation of
  <subject> with ...". The implement loop uses `implementation` for attempt 1 and `repair` (when that ladder exists) afterwards.
- **Per-rung failure rules** (table `ai_rung_rules(task, connection_id, model, rules)`, created idempotently by the store, not a
  numbered migration). Rules follow the rung (keyed by connection + model, so reordering keeps them). Shape:
  `rules: {<kind>: {action: "next"|"wait"|"stop", wait_minutes, max_tries}}`; `next` is the default and is not stored.
  Kinds: `credits_exhausted, usage_limit, rate_limit, auth_failed, model_unavailable, capability_unsupported, context_exceeded`
  (a `ContextWindowExceeded` reported by an adapter), `unreachable, unavailable` (`timeout_not_sent` counts as `unreachable`).
  `wait` is only allowed for `credits_exhausted, usage_limit, rate_limit, unreachable, unavailable`; `0 < wait_minutes <= 240`,
  `1 <= max_tries <= 20`. Saved through `PUT /ai/ladder/{task}`: when any entry carries `rules`, the rules of every rung of that task
  are replaced (an entry without `rules` = defaults); when no entry carries `rules`, stored rules are kept. 400 `{error:{code:"ladder"}}`
  on an unknown kind / action or out-of-range numbers. Entries in `GET /ai/ladder` (and the revision snapshot) carry `rules`;
  `GET /ai/ladder` adds `task_ids`, `rules_meta` (= `GET /ai/rules`: `{failure_kinds:[{kind,label,wait_allowed}], actions, default,
  max_wait_minutes, max_tries}`), `cooldowns`, and per task `chain` (one sentence: "Use claude-x (Claude); if it hits a limit or
  fails, use gpt-x (GPT); then local qwen2.5-coder:14b.").
  Router semantics: `wait` re-sends the SAME rung after `wait_minutes` (bounded by `AIClient.max_rule_wait_s`, default 4 h) up to
  `max_tries` times, only when the failure released its reservation (never after a possibly-billed timeout); each try reserves
  again; when the tries are used up the next rung is tried (no extra default re-send). `stop` raises `StopRequested` (subclass of
  `AllCandidatesFailed`) with a plain `message` ("Stopped and waiting for you: <reason>. Your rule for <model> (<label>) says to
  stop when it <kind> instead of trying the next model. To continue: ..."); the implement loop turns it into stop code
  `stopped_by_rule` and the M-IMPL / M-FIX blocker is that message. Pre-call skips (known capability / context window, policy)
  always move on.
- **Provider cooldown**: when a rung reports `credits_exhausted` or `usage_limit` and the router leaves it (default, after `wait`
  tries, or on `stop`), the connection gets `limits.cooldown = {outcome, reason, set_at, until, until_ts, scope}` for the configured
  minutes (`ai_settings.cooldown_minutes`, defaults credits 1440 / usage 60, 0 = never pause; a provider `Retry-After` wins). Every
  task's `ladder_candidates` then skips it with `skip_outcome: "cooldown"` (reason "<label> ran out of credits; every task skips it
  until <time> (clear the cooldown in Connections ...)"), also inside the same call for other models of that connection. A cooldown
  started by a FREE model (known price 0) has `scope: "free_models"` and pauses only the connection's free models; skipping such a
  rung keeps the free -> paid rules of section 7. The attempt record carries `cooldown_until`, `cooldown_scope`; an `ai.cooldown`
  event is emitted. API: `GET /ai/cooldowns` -> `{settings:{cooldown_minutes}, cooldowns:[{connection_id, connection_label,
  provider, outcome, reason, set_at, until, until_ts, scope, text}]}`; `DELETE /ai/cooldowns/{connection_id}` clears it (state
  `no_credits`/`limited` -> `unprobed`); `PUT /ai/cooldowns/settings {cooldown_minutes:{credits_exhausted?, usage_limit?}}`
  (0..10080). Ladder entries carry `cooldown: null | {..., text}`.
- **Dry run** `POST /ai/route/test {task, case_id?, needs?: ["images"|"tools"], est_input_tokens=8000, max_output_tokens=4096}`
  -> `{task, config_revision, policy_hash, source: "global"|"project"|"policy", sent:false, tokens_spent:0, answer: <position>|null,
  rungs:[Entry + {status: "would_answer"|"standby"|"skipped", outcome, reason, rules_text:[...]}], chain, summary, advisor?}`.
  Nothing is sent, no adapter is created, no budget is reserved and no advisor is called. With `case_id` the project policy
  (mode, overrides, locality, approve_unknown_pricing) and the case budget's remaining amount are applied.
- **JeV advisor** (`providers/jev.py`, replaces the assumed OpenAI-shaped adapter). Contract (the owner's JeV bridge, verified
  against docs.typesafe.ai 2026-10-03): `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`, body
  `{model: "jev-1.13.0", state, questions: {<id>: {type: "choice", instructions, criteria: {<choice>: text}}}}` -> `{model, answers:
  {<id>: {type, choice, probabilities, confidence}}, usage: {input_tokens, output_tokens}}`. Client-side validation (as the bridge):
  type must be `choice`, choice in the allowlist, a finite probability in [0,1] for every allowed choice, distribution sums to 1 +/-
  0.05, confidence (optional; default = top probability) finite in [0,1]; anything else = `invalid_response`. Errors: 401/403
  `unauthenticated`, 422 `invalid_request`, 429 `rate_limited` / 529 `overloaded` (one back-off: `Retry-After` when <= 8 s, else
  0.4 s x 2^n; a longer Retry-After is not waited for), other non-2xx `provider_error`, connect failure retried once then
  `unavailable`, read timeout `timeout` (counted as spent at the reservation, never re-sent). Circuit breaker: 4 consecutive
  failures open it for 60 s, then one half-open probe. Cost: $0.042 per million input tokens (list price, local estimate); every
  request reserves `ceil(len(body)/3)` tokens' worth before sending against `jev:monthly:<yyyy-mm>` (limit = the user's monthly cap,
  default $1, 0-50) or `jev:setup` ($0.05, "Test JeV"), settled at the reported `input_tokens`; every request is an `ai_calls` row
  (`provider: "jev"`, task `jev_advice` | `jev_reassess` | `jev_setup`).
  Questions: rung order - id `route`, choices `R1..Rn` (ladder positions), criteria = descriptors "ladder position i: provider p,
  model m, runs on the user's PC | cloud service, price"; state = task, capability needs, token estimates, candidate count. The
  probabilities rank the usable rungs (ties keep ladder order); confidence < 0.6 keeps the ladder order. Between repair attempts -
  id `next`, choices `RETRY | SWITCH | STOP` (`SWITCH` only when another rung exists); state = task, attempt k of n, build status,
  scenarios passed of declared, current rung descriptor, number of other rungs. `STOP` (confidence >= 0.6) ends the loop with stop
  code `advisor_stop` and a plain message (the best attempt is kept); `SWITCH` moves the last answering rung to the end of the
  next call's order (`AIClient.call(demote=[(connection_id, model)])`); never consulted after a verified attempt, never adds a
  rung, never changes a verdict. Never sent: prompts, code, file names, program output, connection ids or labels.
  Fallback (deterministic order / no advice): off, no key, breaker open, budget exhausted, any error, invalid answer, low
  confidence. Decisions cached by request hash (`<data_dir>/jev/decisions.json`; JeV off ignores the cache); last 50 decisions in
  `<data_dir>/jev/recent.json`; when a case is known, plain activity lines ("JeV suggests trying qwen2.5-coder:14b first
  (confidence 0.82); it only re-orders your rungs", kind `advice`, `provider: "jev"`, `confidence`).
  Key: entered in Connections -> "JeV advisor" and stored in the app credential store (ref `secret:jev-advisor`); never returned,
  logged or written elsewhere. "Use the key from my JeV install" (explicit button) reads only the JeV-documented file
  `JEV_SECRET_FILE` or `<JEV_RUNTIME_DIR | ~/.jev>/secrets/typesafe.key` (first non-empty line, <= 4 KB; `#` / `REPLACE`
  placeholders refused). API: `GET /ai/jev` -> `{enabled, has_key, key_source: null|"entered"|"jev_install", model, endpoint,
  monthly_cap_usd, setup_cap_usd, price, month, setup, breaker:{state, failures, open_until}, offline_reason: null|"off"|"no_key"|
  "breaker_open", min_confidence, jev_install:{key_file_found, path}, last_decisions:[{kind, task, choice, confidence, model, source,
  reason?, spent_usd, at, ...}]}`; `PUT /ai/jev {enabled?, monthly_cap_usd?}`; `PUT /ai/jev/key {key|null}`; `POST /ai/jev/key/import`
  (404 `jev_key_not_found`, 400 `jev_key_invalid`); `POST /ai/jev/test` -> `{ok, reason, message, model?, spent_usd?}`.
  Opt-in live check: `REBUILD_LIVE_JEV=1 pytest -m live tests/test_jev_live.py` (key from `REBUILD_JEV_KEY` or the install file).
