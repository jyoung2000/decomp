# AI providers, budgets and routing (M11)

Checked on **2026-10-06** from a Linux implementation host. Code: `controller/rebuild_controller/providers/`, `controller/rebuild_controller/budget.py`.
Tests: `controller/tests/test_providers.py test_budget.py test_router.py test_secrets.py test_jev.py`.

## What is and is not certified

All default tests use `httpx.MockTransport`, scripted `MockProvider`s and fake CLI runners. **Passing them does not certify any live account,
model, plan or endpoint.** They prove request shapes, stream parsing, error typing, budget arithmetic and routing logic against shapes we
believe the vendors use. Live behaviour is checked only on demand (see "Opt-in live checks"). Default tests never spend money.

## Documentation reachability (2026-10-06)

| Source | Result | Consequence |
|---|---|---|
| platform.claude.com (pricing, streaming, structured outputs, models list) | read | Anthropic adapter and price table are sourced from it |
| code.claude.com (Agent SDK overview, CLI reference) | read | `claude_agent_sdk` handoff is `supported=False` (policy text below) |
| raw.githubusercontent.com openai/codex README + `codex-rs/exec/src/cli.rs`; google-gemini/gemini-cli README | read | `openai_siwc`, `gemini_cli` handoff modes |
| developers.openai.com, platform.openai.com, api.openai.com | **unreachable** - docs unreachable on 2026-10-06; verify | OpenAI Responses/chat adapter written from the stable, well-known shapes |
| ai.google.dev, generativelanguage.googleapis.com | **unreachable** (403 from the proxy) - docs unreachable on 2026-10-06; verify | Gemini adapter written from the stable, well-known REST shapes |
| openrouter.ai | **unreachable** - docs unreachable on 2026-10-06; verify | OpenRouter served through the chat dialect; `usage.cost` / `/models` pricing handling is unverified |
| JeV | no API documentation available | endpoint shape is an explicit assumption (below) |

Items marked "verify" must be exercised once against the real service (`pytest -m live`, or a manual probe from the Connections screen) before being relied on.

## Access method per provider

| Provider (`provider`) | Connection `auth_mode` | Class | Endpoint / request | BYOK or handoff |
|---|---|---|---|---|
| OpenAI | `api_key` | `OpenAIResponsesAdapter(dialect="responses")` | `POST {endpoint or https://api.openai.com/v1}/responses`, `store:false`, reasoning items replayed verbatim (`include: reasoning.encrypted_content`) | BYOK |
| OpenAI (chat) | `api_key` | same, `dialect="chat"` | `POST .../chat/completions`, `stream_options.include_usage`, `max_completion_tokens` | BYOK |
| OpenRouter | `api_key` | same, `provider="openrouter"`, default `dialect="chat"` | `https://openrouter.ai/api/v1/chat/completions`; requests `usage:{include:true}` and uses a reported `usage.cost` when present | BYOK |
| Local OpenAI-compatible (LM Studio, Ollama, llama.cpp, vLLM) | `local` / `none` | same, `provider="local"`, `dialect="auto"` | endpoint is mandatory (never defaulted); probe tries `/responses` then `/chat/completions` and stores the winner in `limits.dialect` | n/a (no key, zero metered cost assumed) |
| Anthropic | `api_key` | `AnthropicAdapter` | `POST {endpoint or https://api.anthropic.com}/v1/messages`, headers `x-api-key`, `anthropic-version: 2023-06-01`, optional `anthropic-beta` (`limits.betas`) | BYOK |
| Gemini | `api_key` | `GeminiAdapter` | `POST .../v1beta/models/{model}:streamGenerateContent?alt=sse`, header `x-goog-api-key` (key never in the URL) | BYOK |
| OpenAI plan | `subscription_handoff` | `subscription.get_mode("openai_siwc")` | user's own `codex exec -`, task on stdin | **handoff**, `supported=True` |
| Gemini plan | `subscription_handoff` | `subscription.get_mode("gemini_cli")` | user's own `gemini -p <fixed text>`, task on stdin | **handoff**, `supported=True` |
| Claude plan | `subscription_handoff` | `subscription.get_mode("claude_agent_sdk")` | **not offered** | **handoff, `supported=False`** |

### Anthropic specifics (from the claude-api skill and platform.claude.com, 2026-10-06)

* Thinking: Fable 5.x / Mythos 5.x / Opus 5.5 are always-on (no `thinking` field; `display:"summarized"` only when a summary is requested);
  Opus 5 / 4.6-4.8 and Sonnet 5 / 5.5 / 4.6 use `{"type":"adaptive"}`; Haiku 4.5 and older use `budget_tokens` (>=1024, < `max_tokens`).
  **Unknown model ids get no thinking field** unless `Reasoning.mode` is set - nothing is guessed. Effort goes in `output_config.effort`.
* JSON schema: `output_config.format = {"type":"json_schema","schema":...}`.
* Forced `tool_choice` (`any`/`tool`) is refused client-side for Fable 5.1 / Mythos 5.1 / Opus 5.5 / Sonnet 5.5 (documented 400). `temperature` is dropped (with a note on the response) for the model families documented to reject sampling parameters.
* Prompt caching: explicit `cache_control` on the last system block and last tool (5m, or `cache_ttl="1h"`); no beta header is required at the time of writing. Cache read/write tokens are priced separately and appear in `Usage.cached_tokens` / `cache_write_tokens`.
* Returned content blocks (thinking + signature, `redacted_thinking`, `tool_use`, server-tool blocks) are kept verbatim in `Response.provider_blocks` and replayed unchanged to the same adapter family. Replayed to another provider they degrade to typed text/tool parts (reasoning is dropped).
* The SDK is deliberately not used: adapters share one injectable `httpx` transport so tests never touch a network, and the project has one HTTP code path for all providers. The raw-HTTP shapes follow the skill's cURL reference.

### Subscription handoff (what "supported" means here)

A handoff is **the user's own vendor CLI, signed in by the user, started with a task file**. Rebuild Studio never reads CLI session or credential
files, never scrapes sessions, never proxies or relays subscription tokens, and never puts task text on a command line (it goes to stdin, so there is no
shell quoting surface). The child process gets `isolated_env()`: only neutral variables, no `*_API_KEY`/`TOKEN`/`SECRET`, no `JEV_*`, so a subscription
login cannot silently become a BYOK charge. `launch_external` returns a structured `LaunchResult`; a vendor limit message becomes `UsageLimit`
via `result.raise_for_usage_limit()` (subscription limits are **not** `RateLimit` and are not retried).

| Mode | supported | Basis (checked 2026-10-06) | Gaps |
|---|---|---|---|
| `openai_siwc` | true | openai/codex README: "Run `codex` and select Sign in with ChatGPT ... Plus, Pro, Business, Edu, or Enterprise plan"; `codex exec [PROMPT]` with `-` reading stdin from `codex-rs/exec/src/cli.rs` | developers.openai.com unreachable: plan limits and terms for scripted use not read |
| `gemini_cli` | true | google-gemini/gemini-cli README: "Sign in with Google (OAuth login using your Google Account)", `gemini -p` headless | auth guide / headless pages not read; stdin + `-p` combination not confirmed - verify with `gemini --help` |
| `claude_agent_sdk` | **false** | Agent SDK overview: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods" | approval status unknowable from docs; `launch_external(..., vendor_approved=True)` exists only for a user who holds such approval |

Subscription quotas are never invented: `BudgetLedger.get_quota(connection_id)` returns `quota_known: false` and `usd_tracked: false` until a
user enters a quota explicitly (`set_quota(..., quota_known=True, unit=..., limit=...)`). Quotas live in `meta` (`quota:<id>`), never in dollar budgets.

## Models: discovery or explicit ids, never invented

* `discover_models()` exists per adapter (`/v1/models` for Anthropic and OpenAI-style, `/v1beta/models` for Gemini, paginated and bounded). It returns `ModelDiscovery(supported, models, error)`; an endpoint without a models route yields `supported=False`, not an empty "success".
* Connection model entries are `{"id", "source": "discovered"|"explicit", optional "price"}`. A probe replaces the discovered entries and keeps explicit ones.
* No model id is ever derived from a provider name or label. A route must name a model; once discovery has worked the model must be among the discovered ones (or `allow_unlisted=True`).

## Capability probing (no full compatibility assumed)

`probe_capabilities(model)` sends up to four small requests (basic stream, a tool call, a JSON-schema answer, a 16x16 red PNG) and records, per capability:
`supported`, `rejected` (HTTP 4xx), `accepted_no_call`, `accepted_invalid_output`, `accepted_unverified` (image accepted but the colour was not read back), `not_reported`
(usage absent from the stream), `error`, `untested`. `resolve()` excludes a connection only for `rejected`/`unsupported`; inconclusive results never exclude.
Probes are metered through the ledger (budget `probe:<connection_id>`, $0.50 lifetime), and are **skipped for models with unknown pricing** unless `approve_unknown_pricing=True`.
Connection state: `unprobed | ok | auth_failed | unreachable | limited` (a live call updates it too).

## Pricing

`pricing.PriceTable`; every entry carries `known`, `source`, `checked_on`. Unknown pricing is never zero: it resolves to a **conservative explicit limit**
($30 / $150 per million input/output tokens, above every listed model) with `approval_required=True`; a call needs `approve_unknown_pricing=True`
and the ceiling reservation must still fit the budget. Settlement on an unpriced-but-approved call uses the conservative rate and records `cost_known=0`.

| Models | Source | Checked |
|---|---|---|
| Anthropic: Fable 5.1/5, Mythos 5.1/5, Opus 5.5/5/4.8/4.7/4.6/4.5/4.1/4, Sonnet 5.5/5/4.6/4.5/4, Haiku 4.5/3.5 (input, output, 5m/1h cache write, cache read) | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-06 |
| OpenAI, Gemini, OpenRouter | **not hard-coded** (price pages unreachable). Set `price` on a connection model entry, or let OpenRouter `/models` pricing and `usage.cost` supply it | - |
| Local endpoints | zero, `known=True`, source "local endpoint: no metered cost assumed" (override with a model price if the endpoint is actually metered) | - |

## Budgets

`BudgetLedger` over `budgets`/`reservations`: `reserve` is atomic (`BEGIN IMMEDIATE`), counts held money against the limit, and rejects a repeated `request_key`
(`UNIQUE`) with `DuplicateReservation` - also after settle, so a settled request cannot be paid twice. `settle` records actual spend (an overrun above the ceiling is
recorded and the budget then reads exhausted, never clamped); `release` returns held money; `sweep_stale(max_age_s)` settles abandoned holds at their **full** amount
(we cannot prove the provider did not bill). Emits `budget.updated`. Scopes: `job:<id>`, `case:<id>`, `jev:setup`, `jev:monthly:<yyyy-mm>`, `probe:<connection_id>`.

## AIClient call path (`router.py`)

resolve route (primary then fallbacks, stored order) -> optional JeV reorder -> price -> **reserve** -> send -> **settle actual** -> `ai_calls` row -> `ai.call` event.

* Nothing is sent before a reservation succeeds; an exhausted budget sends nothing (and a cheaper fallback may still fit).
* A paid call with `budget=None` is refused (`BudgetRequired`); zero-cost local calls need no budget.
* Retry rule: at most **one** re-send, and only when the failure confirms the request was not processed (`retry_safe`: 429 rate limit, connect failure/timeout).
  Read timeouts and cut-off streams (`AmbiguousCompletion`) are **never** re-sent to the same endpoint and are settled at the full reservation. Moving to the next configured fallback is bounded by the route length, each with its own reservation (`AIClient(fallback_on_ambiguous=False)` stops instead).
* Outcomes recorded in `ai_calls.outcome`: `ok`, `auth_failed`, `usage_limit`, `rate_limit`, `unreachable`, `invalid_request`, `unavailable`, `timeout`, `timeout_not_sent`, `ambiguous`, `budget_exhausted`, `approval_required`, `internal_error`.
* Missing usage in a stream settles at the reservation with `cost_known=0`; a provider-reported charge (OpenRouter) wins.
* Idempotency: pass a stable `request_key` for work that must never be paid twice (default is a fresh key per call).

## Secrets

`SecretStore`: Windows uses DPAPI (`CryptProtectData`, user scope, fixed entropy) via ctypes; elsewhere a `0600` file under `<data_dir>/secrets` with a
`SecretStoreWarning` that it is **not OS-backed**. The DB stores only an opaque `secret:<id>` reference. `redact()` masks every stored/registered secret plus common key shapes
and is applied to error text, logs (`RedactingFilter` on `rebuild.*` loggers), events and CLI output. `isolated_env()` builds provider-subprocess environments.
**Windows gate:** the DPAPI path could not be executed on this Linux host (only its selection logic and the entry/backend plumbing are tested); run `pytest tests/test_secrets.py` plus a manual put/get on Windows before release.

## JeV advisory router (optional)

Environment only: `JEV_API_KEY` (never stored), `JEV_ENDPOINT`, `JEV_MODEL`, optional `JEV_PRICE_INPUT_PER_MTOK` / `JEV_PRICE_OUTPUT_PER_MTOK`. Every decision carries `unverified: true`.
**Assumption:** no JeV API documentation was available, so `JEV_ENDPOINT` is treated as an OpenAI-compatible `/chat/completions` base URL that accepts `response_format: json_schema`
and returns `{"order":[...],"confidence":0..1,"reason":"..."}`; any other shape falls back. Caps are fixed in code ($0.05 `jev:setup`, $1.00 `jev:monthly:<yyyy-mm>`) and cannot be raised from the environment.
Only task name, capability needs, token estimates and candidate descriptors are sent - never case content. Decisions are cached by request hash in `<data_dir>/jev/decisions.json` (atomic writes, bounded, corrupt files quarantined).
No key, no endpoint, an HTTP/JSON failure, low confidence (< 0.6), invalid advice or an exhausted cap all return the deterministic order. JeV can only reorder candidates the deterministic router already resolved.

## Opt-in live checks

Never part of the default run. Live tests also require `-m live` (a stray key in your shell cannot trigger spend):

```
export ANTHROPIC_API_KEY=...   LIVE_ANTHROPIC_MODEL=<exact model id>
export OPENAI_API_KEY=...      LIVE_OPENAI_MODEL=<exact model id>
export GEMINI_API_KEY=...      LIVE_GEMINI_MODEL=<exact model id>
cd controller && /opt/rebuild-tools/venv/bin/python -m pytest -m live tests/test_providers.py -q
```

Each makes one call capped at 16-32 output tokens. The model id must be supplied explicitly - no model is guessed.

## Limitations

* OpenAI, Gemini and OpenRouter shapes are from memory of the public APIs (docs unreachable); field names such as Gemini `responseJsonSchema` / `parametersJsonSchema` / `thinkingLevel` and OpenRouter `usage.include` need live confirmation. `GeminiAdapter(schema_field="parameters")` selects the older tool-schema field.
* Gemini function-call ids are synthesized locally (`gemini-call-N`) when the API supplies none and are never sent back.
* Token estimates for reservation ceilings are deliberately generous (3 chars/token, 2000 tokens/image); they are never used for billing.
* Stream usage for local servers is often absent: such calls report `usage.known=False`; they cost zero only when the connection is a local endpoint.
* Cost tracking is per request from reported usage; provider-side billing (batch discounts, regional multipliers, fast mode) is not modelled.
