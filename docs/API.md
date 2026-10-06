# Controller API contract (UI ⇄ controller)

Transport: loopback HTTP (`http://127.0.0.1:<port>`) + WebSocket `/ws`. The port and a per-launch token are written to
`<data_dir>/controller.json` (`{"port":..,"token":..,"pid":..}`) by the sidecar; the Tauri shell reads it and injects both into the UI
via `window.__REBUILD_STUDIO__ = {baseUrl, token}` (dev: Vite proxy with `VITE_CONTROLLER_URL`/`VITE_CONTROLLER_TOKEN`).
Every request carries `Authorization: Bearer <token>`. Requests whose `Origin` is not the app origin / `http://localhost:*` / `tauri://localhost` are refused (403).
All JSON. Timestamps ISO-8601 UTC. Errors: `{"error": {"code": str, "message": str, "affected": str?, "next_action": str?}}`. Status mapping: 401 auth, 403 origin/permission, 404 unknown id, 400 validation/path policy, 402 BudgetExhausted, 409 ApprovalRequired (unknown pricing), 424 NoRoute/AllCandidatesFailed, 503 optional service unavailable.

## Events (WebSocket `/ws?token=..&since=<seq>`)
Server sends `{"seq", "ts", "case_id", "job_id", "kind", "payload"}` objects, one per message, strictly increasing `seq`.
On connect, the server replays events with `seq > since` then streams live. Clients MUST dedupe on `seq`.
`controller.heartbeat` is emitted at least every `lease_timeout_seconds` (default 30 s; `GET /health` returns the value). If no
event (any kind) arrives within 2× that interval the UI MUST mark state stale/unknown.
Kinds: `case.created|case.status`, `job.created|started|progress|log|retry|unblocked|blocked|completed|failed|cancelled|cancel_requested|resumed|needs_retest|lease_expired`,
`evidence.added|invalidated`, `plan.revised|plan.item`, `feature.updated`, `candidate.created|built|failed`, `comparison.recorded`,
`preview.published|stale`, `feedback.created|updated`, `budget.updated`, `ai.call`, `knowledge.updated`, `controller.heartbeat`.

## REST
- `GET /health` → `{ok, version, pid, heartbeat_seconds, latest_seq, started_at}`
- `GET /events?since=N&case_id=` → `[event]` (same shape as WS; bounded 5000)
- `GET /doctor?smoke=0|1` → backend availability report (`missing|detected|installed|usable|verified` per tool)
- `GET /cases` / `POST /cases` body `{name, source_root, output_root, target_language: rust|rust_bevy|web|auto, output_type: exe|installer|portable|web|pwa, ai_policy:{mode: no_ai|assist_on_failure|assisted, budget_usd?}, launch_profile:{execute_original: bool, command?: [..], scenarios?: [..]}}`
- `GET /cases/{id}` → case + counts (`jobs` by state, features by status, candidates, previews, open feedback)
- `POST /cases/{id}/start` → `{job_ids}`; `POST /cases/{id}/pause`; `POST /cases/{id}/resume`; `POST /cases/{id}/cancel`
- `POST /cases/{id}/deliver` body `{candidate_id?}` → deliver job (default: last known good, else newest built candidate)
- `POST /features/{id}/review` body `{review: accepted|rejected|null}` — user acceptance, separate from machine verification
- `GET /cases/{id}/jobs` → `[job]` with `progress` (raw counts + denominators, never synthesized %), `blocker`, `attempt`, `heartbeat_at`
- `POST /jobs/{id}/cancel` / `POST /jobs/{id}/resume`
- `GET /cases/{id}/modules`, `GET /cases/{id}/evidence?kind=&module_id=`, `GET /evidence/{id}?max_bytes=`, `GET /cases/{id}/evidence/search?q=`
- `GET /cases/{id}/features` → feature ledger rows (`impl_status`, `verify_status`, `user_review`, `critical`, `verify_candidate`)
- `GET /cases/{id}/plan` → `{revision, items:[item], unknown_scope:[...], progress:{analysis:{done,total|null}, recovery:{..}, implementation:{..}, build:{..}, verification:{..}}, eta: {seconds, uncertainty, updated_at}|null}`
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
