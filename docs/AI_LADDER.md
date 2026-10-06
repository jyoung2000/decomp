# AI model ladder, local AI and plan visibility — contract

Status: contract for the P13 work (controller + UI). Extends the existing router (`providers/router.py`), connection store
(`providers/connections.py`, tasks `interpretation, repair, visual_review, verification_assist, knowledge`) and routes API
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
