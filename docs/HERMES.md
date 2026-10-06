# Hermes computer-use bridge (M14)

Code: `controller/rebuild_controller/hermes/{bridge,protocol}.py`. Tests: `controller/tests/test_hermes.py`
(`cd controller && /opt/rebuild-tools/venv/bin/python -m pytest -q tests/test_hermes.py`).

The Hermes docs site (hermes-agent.nousresearch.com) is unreachable from the Linux build host, so everything below was
derived from the pinned source, not from documentation and **not from a live Windows run**.

## 1. Source pin

| Item | Value |
|---|---|
| Repo | `https://github.com/NousResearch/hermes-agent` |
| Pinned commit | `daefc2b735ea32a729b026e0116c1fc6bf5980d1` (2026-10-06 00:10 -0700, "map contributor email for honcho") |
| `/opt/rebuild-tools/hermes-src` HEAD when inspected | `060059a8814f2b1184de0a82a3bd8c17647a4770` (a later commit; the pin was fetched separately with `git fetch --depth 1 origin daefc2b...`) |
| Cited files that differ between the two commits | none (checked with `cmp`), so every `path:line` below holds for both |
| Hermes version string | `pyproject.toml:7` says `0.0.0`; the real string is computed at runtime, see 2.1 |
| cua-driver pin (Hermes package manager) | `0.21.0` (`pm/lock.json:78`, `pm/packages.py:867-901`); Hermes' hard floor is `0.20.0` (`tools/computer_use/cua_backend_driver.py:23`) |

To re-check against the pin: `git -C /opt/rebuild-tools/hermes-src fetch --depth 1 origin daefc2b735ea32a729b026e0116c1fc6bf5980d1 && git -C /opt/rebuild-tools/hermes-src checkout FETCH_HEAD`.

## 2. Contracts verified from source

### 2.1 CLI entry points
- Console script `hermes = hermes_cli.main:main` (`pyproject.toml:564`); also `hermes-agent` (`agent.legacy_cli:main`) and `hermes-acp`.
- `hermes --version` / `-V` prints `Hermes Agent v<version> (<release date>)` (`hermes_cli/_parser.py:147`, `hermes_cli/_startup_fast.py:202`). The bridge parses exactly that line.
- Non-interactive single-query session: `hermes chat -q TEXT | --query-file PATH [-Q] [--format text|stream-json]`
  (`hermes_cli/_parser.py:231,235,262,264`). `--query-file` reads the prompt from a file (or `-` = stdin), "safe for arbitrary text".
  A top-level `-z/--oneshot PROMPT` also exists (`_parser.py:148`) but also auto-bypasses approvals (`tools/computer_use/tool.py:146-157`) so the bridge does **not** use it.
- Flags the bridge relies on (all documented in `_parser.py`): `-p/--profile NAME` (pre-parsed from argv, `_parser.py:16`, `hermes_cli/main.py:593-650`),
  `-t/--toolsets` (`:173`), `--max-turns N` (`:290`), `--run-budget SECONDS` (`:292`), `--source tool` ("third-party integrations that should not appear in user session lists", `:309`),
  `-m/--model` and `--provider` (`:164`, `:259`), `--ignore-rules` (`:305`). `--yolo` (`:299`) exists and is **never** passed.
- `hermes computer-use install|status|doctor [--json]` (`hermes_cli/subcommands/computer_use.py:34,76,145`); `hermes mcp add|remove|list|test|configure` (`hermes_cli/subcommands/mcp.py:27+`).

### 2.2 Home / profile layout
- Home resolution: context override, then `HERMES_HOME`, then platform default: `%LOCALAPPDATA%\hermes` on native Windows, `~/.hermes` elsewhere incl. WSL2
  (`hermes_constants.py:51-61`, `:111-121`; `README.md:61`). `HERMES_DATA_DIR_SUFFIX` appends to the leaf.
- Named profiles live at `<root>/profiles/<name>/`; `<root>/active_profile` is a sticky selection (`hermes_constants.py:231`, `hermes_cli/main.py:600-625`).
  **Pitfall:** `HERMES_HOME` pointing at the *root* does not pin the profile: Hermes still reads `active_profile` and may switch. The bridge therefore always passes `-p <name>`
  (`default` for a root) plus `HERMES_HOME` (`main.py:607` trusts `HERMES_HOME` only when its parent directory is `profiles`).
- Profile names match `^[a-z0-9][a-z0-9_-]{0,63}$` (`main.py:431`). A directory is a Hermes profile if it contains any of `config.yaml .env SOUL.md profile.yaml auth.json state.db` (`hermes_constants.py:318`).
- Config file: `<home>/config.yaml` (`hermes_constants.py:1190`); credentials: `<home>/.env`, `<home>/auth.json` (never read or written by the bridge).
- Logs: `<home>/logs/agent.log`, `errors.log` (`hermes_logging.py:3`). The bridge records new `agent.log` bytes written during a run.
- Screenshots persisted by the computer-use tool: `<home>/cache/images/computer_use_<uuid>.png` (legacy `<home>/image_cache/`), newest 20 kept (`tools/computer_use/tool.py:562,823-829`).

### 2.3 `mcp_servers` config format
`<home>/config.yaml`, top-level `mcp_servers:` mapping of server name to settings (`cli-config.yaml.example:1582`,
`skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md:30-70`):

```yaml
mcp_servers:
  rebuild_studio:
    command: rebuild-mcp              # stdio transport; or `url:` for HTTP (exactly one of the two)
    args: []
    env: {REBUILD_STUDIO_DATA: "..."}  # ONLY these env keys reach the server (native-mcp.md:169)
    timeout: 120
    connect_timeout: 60
```
Tools are registered as `mcp_<server>_<tool>` (hyphens/dots become `_`, `native-mcp.md:106`) and auto-injected into every platform toolset (`:116`).
`register_mcp` edits only this one entry (see 4.2).

### 2.4 Session output (`--format stream-json`)
`hermes_cli/stream_json.py:1-103`: one JSON object per stdout line, each with `timestamp` (ms): `{"type":"system","subtype":"init","model","session_id"}`,
`{"type":"text","text"}`, `{"type":"tool_use","name","tool_call_id"?,"input"}`, `{"type":"tool_result","name","tool_call_id"?,"output"(<=5000 chars, then "..."),"duration_ms","is_error"}`,
terminal `{"type":"result","session_id","exit_code","text","tokens":{input,output,total,cache_read,cache_write},"duration_ms","error"?}`.
The `session_id` line also goes to **stderr** (`\nsession_id: <id>`, `:95`). `stream-json` requires `-q`/`--query-file`, implies quiet, and cannot combine with `--tui` (`:18-30`).

### 2.5 Approvals in non-interactive runs (important)
A `-q` run sets `HERMES_SINGLE_QUERY_SESSION=1` (`hermes_cli/cli_single_query.py:514`). Every `computer_use` input action goes through the approval gate
(`tools/computer_use/tool.py:384-404`, `_ACTIONS` `:464-480`) and, with nobody to answer, **fails closed** unless the user's profile says otherwise: default
`approvals.single_query_mode: deny` (`hermes_cli/config_defaults.py:1683`; `tools/approval.py:607,956-1010`). Captures/list actions are not gated.
Opt-ins live in the **user's** profile: `approvals.single_query_mode: approve`, or `computer_use.permission_mode: bounded` + a reviewed `capability_manifest`
(`config_defaults.py:2588-2591`; manifest schema is documented only on cua.ai and is **not** in the source, so the bridge never generates one).
The bridge never edits these and never passes `--yolo`; `status()` raises `approval_blocks_unattended` and a blocked action raises `approval_blocked`.

### 2.6 The `computer_use` tool
One tool with an `action` enum (`tools/computer_use/schema.py:20-35`): `capture click double_click right_click middle_click drag scroll type key set_value wait list_apps list_windows focus_app`.
`capture` takes `mode` `som|vision|ax` (`:45`), `app` (`'screen'`/`'desktop'` = whole screen / shell), `pid`+`window_id`. Input actions take `delivery_mode` `background|foreground` (`:159`).
Hard blocks in the tool layer (lock screen, log out, `curl|bash`, `sudo rm -rf`, fork bomb) are at `tool.py:39-66`; the bridge mirrors them in direct mode (`protocol.reject_unsafe`).
Results: text-only actions return a JSON string `{ok, action, message?, verified?, effect?, escalation?, path?, degraded?, delivery_mode?, code?, meta?, verdict}` (`tool.py:523-552`);
`effect` is `confirmed|unverifiable|suspected_noop`. Captures with an image return a multimodal dict (`tool.py:3,691`) whose `str()` is what stream-json prints,
**truncated at 5000 chars, so the trailing `meta.screenshot_path` is usually lost** (see 6).
Hermes' own backend refuses an action the driver cannot do with `code="unsupported_action"` (`tools/computer_use/backend.py:184`).

### 2.7 cua-driver and its MCP bridge
- Pinned binary, installed by Hermes' package manager (`hermes computer-use install`): `cua-driver-rs-v0.21.0` release archives per OS/arch, Windows `windows-x86_64`/`windows-arm64` zip
  containing `cua-driver.exe` (`pm/packages.py:867-901`, `pm/lock.json:78`). Override with env `HERMES_CUA_DRIVER_CMD` (`cua_backend_driver.py:20,75-85`). Install hint at `:91`.
  The Windows archive "carries its runtime helpers (including Windows UIAccess)" (`pm/packages.py:893`); install notes mention `cua-driver-uia.exe` (`hermes_cli/tools_config_cua.py:261`).
- Launch: `<cua-driver> mcp` is a **stdio MCP server** (`cua_backend_driver.py:22`); the authoritative command is `cua-driver manifest` -> JSON `{binary_version, mcp_invocation:{command,args}, subcommands:[{name,args:[{name}]}]}`
  (`:110-136`). Hermes' runtime contract: `binary_version >= 0.20.0`, an MCP launch command, and the flags `mcp --socket --grant`, `serve --socket --permission-mode --capability-manifest --approve-capability-manifest --embedded`, `stop --socket` (`:23-28,138-158`). The bridge mirrors this check.
  `--no-overlay` is appended when supported (`:95-108`); the cursor overlay can spin a core (`config_defaults.py:2584`).
- Hermes talks to it with the Python `mcp` SDK (`ClientSession` over `stdio_client`, `cua_backend_session.py:236`), then `tools/list` to learn per-tool schemas and `capability_version` (`:265-290`),
  then `start_session {session}` (`cua_backend.py:316-318`); `end_session` on stop (`:335`). Fallback transport: `cua-driver call <tool> <json>` against a machine-wide daemon (`cua_backend_session.py:96,437`).
- Tool names and arguments the backend sends (all also carry `session`, except `bring_to_front`):

| Tool | Arguments (source) |
|---|---|
| `list_windows` | `on_screen_only`; result `windows[] {app_name,pid,window_id,title,is_on_screen,z_index}` (`cua_backend_capture.py:175`, `cua_backend_parse.py:225-261`) |
| `get_window_state` | `pid, window_id, max_elements?` -> **UI tree + screenshot**: `structuredContent.elements[] {element_index, role, label, frame{x,y,w,h}, element_token}` (preferred) or markdown `- [N] Role "label"` (`capture.py:244-303`, `parse.py:18-30,97-121`) |
| `screenshot` | `window_id, format, quality` - only if advertised (`capture.py:266-267`) |
| `get_desktop_state` | whole-screen grab, pixels only (`capture.py:333-370`) |
| `click` / `double_click` | `pid, window_id, element_index`+`element_token` **or** `x,y`; `button`; `modifier[]`; `delivery_mode` (`cua_backend_input.py:89-111`) |
| `type_text` | `pid, window_id, text` (`input.py:148-151`) |
| `press_key` / `hotkey` | `key` / `keys[]` (modifiers + key) (`input.py:153-162`) |
| `scroll` | `direction, amount (1..50), element_index?|x,y?` (`input.py:128-146`) |
| `drag` | `from_element,to_element` or `from_x,from_y,to_x,to_y` (`input.py:113-126`) |
| `set_value` | `element_index, value` (`input.py:164-171`) |
| `bring_to_front` | `pid, window_id` (no `session`; `cua_backend.py:382-387`) |
| `get_config` / `set_config` | `key`, `value` (`capture.py:344-349`) |
| `start_session` / `end_session` | `session` |

  Recorded `tools/list` fixture (12 selected tools of 49, driver epoch 0.9): `tests/fixtures/cua_driver_0_9_tools_list.json`. There is **no** separate "UI tree" tool: the tree comes with `get_window_state`.
- Result envelope: MCP `{content:[{type:text|image,...}], structuredContent, isError}` (`cua_backend_parse.py:158-188`). Never treat `isError:false` as proof of effect; read `structuredContent.effect`.
  Calls whose outcome is unknown (timeout, broken pipe) are **never replayed** (`cua_backend_session.py:71-96,468-521`); `list_windows`, `get_window_state`, `list_apps` are the only replay-safe tools (`:181`). The bridge copies this rule.
- Environment: Hermes strips provider API keys from the driver's env and sets `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` (upstream telemetry defaults ON) (`cua_backend.py:137-184`, `config_defaults.py:2553`). The bridge does the same.
- Linux/headless diagnosis Hermes already does: no `DISPLAY` -> "X11/XWayland is not reachable"; locked session via `loginctl LockedHint` (`cua_backend.py:218-246`).

### 2.8 Native Windows notes
- Hermes runs natively on Windows (no WSL): install `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`; home `%LOCALAPPDATA%\hermes` (`README.md:43-61`).
- cua-driver on Windows uses UI Automation and may spawn a UIAccess worker (`tools_config_cua.py:261`); SmartScreen may prompt on first run.
- **Session 0**: an SSH/service session has no interactive desktop; Hermes offers an opt-in per-boot `cua-driver-serve` logon task, `computer_use.autostart: true`
  (`config_defaults.py:2559`, `tools_config_cua.py:195-247`, `skills/autonomous-ai-agents/computer-use/SKILL.md:303,346`). The cua-driver `WINDOWS.md` deep-dive is referenced but not in the Hermes repo.
- Known Windows failure: Hermes' venv interpreter cannot execute a binary under `C:\Program Files\WindowsApps`; set `HERMES_CUA_DRIVER_CMD` to a copy outside it (`SKILL.md:310`).
- Hermes has **no** documented machine-identity or UIPI/elevation API; those diagnostics are the bridge's own (4.4).

## 3. Two modes - never conflated

| | `hermes_agent_session` | `cua_driver_direct` |
|---|---|---|
| Who decides actions | The user's Hermes model, in the user's paired profile | A recorded recipe (no model) |
| What runs | `hermes -p <profile> chat -Q --format stream-json --query-file ... --max-turns N` | `cua-driver mcp` spoken to directly by `DirectDriverSession` |
| Credentials / provider | Stay in the user's Hermes profile; screenshots go to the model provider selected there | None; no network; driver env scrubbed, telemetry off |
| Scope enforcement | Observed from the stream; violation kills the process tree (not a pre-execution gate, see 6) | Enforced **before** anything is sent |
| Label | `mode: "hermes_agent_session"` on every record, task result and evidence row | `mode: "cua_driver_direct"`; result text says "NOT a Hermes integration" |

## 4. What the bridge does

### 4.1 `detect_installation()`
Finds `hermes` on `PATH`, runs `hermes --version`, resolves the home/profile dir and lists named profiles. Missing returns `state: "missing"` plus `next_action` (the install command for the platform + `hermes setup`; the
bridge never runs an installer). `broken` = the binary exists but `--version` fails.

### 4.2 `pair(profile_path=None)` and `register_mcp(dry_run=True)`
`pair` records `{profile_path, profile_name, hermes_path, version, host, platform, paired_at}` in `<data_dir>/hermes/pairing.json`. It requires an existing directory that looks like a Hermes profile, outside Rebuild
Studio's data dir, and never creates or edits a profile. `register_mcp` merges `mcp_servers.rebuild_studio` into the paired `config.yaml`:
text-preserving (comments, ordering, CRLF/BOM kept) for the usual shapes, a verified full re-dump (`comments_preserved: false`) for flow-style `mcp_servers`; the result is re-parsed and compared with the expected data structure
before anything is written; a unified diff is returned; a timestamped `config.yaml.rebuild-studio-backup-*` is written before any change; a second call is a no-op (no write, no backup). A missing `config.yaml` is refused (`hermes setup` creates it).
The entry never contains secrets (only `REBUILD_STUDIO_DATA`). PyYAML is required (transitively present via `uvicorn[standard]`; declare it in `pyproject.toml` when M14 is wired up).

### 4.3 `run_task(HermesTask)` (agent session)
Writes `<data_dir>/hermes/tasks/<id>/task.json` (scope: allowed actions, target window title/regex, target processes, max steps, model, provider; **no credentials**: such keys/values are rejected) and `prompt.md`,
launches Hermes via a streaming runner that registers its process with the job's `StageContext` (so job cancel kills the tree; `taskkill /F /T` on Windows, process group on POSIX), parses stdout line by line,
and returns a `TaskResult` with action records:

```json
{"seq": 2, "mode": "hermes_agent_session", "source": "hermes_stream", "action": "click", "args": {"element": 2},
 "pre_screenshot_sha": "<sha256>", "result": {"ok": true, "effect": "confirmed", "code": null, "verdict": {...}, "message": "..."},
 "post_screenshot_sha": "<sha256>|null", "postcondition_ok": true, "ts": "2026-10-06T12:00:00.123Z"}
```
`postcondition_ok` is `true` for `effect: confirmed`/`verified`, `false` for `suspected_noop` or a refusal code, `null` when unproven. Typed text is redacted (length + sha256 only). With a `CaseStore` and `case_id`,
evidence kinds `hermes_action_log` (records, diagnostics, redacted raw events, mode, task hash) and `hermes_screenshot` (the PNG, content-addressed) are stored with `producer="hermes-bridge"`.
Statuses: `completed failed cancelled disconnected timeout scope_violation`. `connected=` returning false terminates the tree cleanly (`disconnected`).

### 4.4 `status()` and diagnostics
Returns `host`, `session` (Windows: `session_id` via `ProcessIdToSessionId`, window station, input-desktop probe via `OpenInputDesktop`; elsewhere `DISPLAY`/`WAYLAND_DISPLAY`), `hermes`, `pairing`,
`driver` (path, version, contract check, `mcp_invocation`, subcommands), `capabilities` (action enum, driver tools; `probe_tools=True` adds the live `tools/list` and `capability_version`), `modes.*.ready`, and `diagnostics[]`:

| Code | Raised when |
|---|---|
| `no_interactive_windows_session` | bridge host is not Windows (this Linux host) or WinSta0 is not the window station |
| `session_0` | `ProcessIdToSessionId == 0` |
| `desktop_locked` | `OpenInputDesktop` fails (heuristic; Session 0 reports `session_0` instead) |
| `uipi_elevated_target` | target pid elevated (or token unreadable) while the bridge is not; or driver text says "Access is denied/UIPI" |
| `driver_missing` / `driver_contract_unmet` | no cua-driver / fails Hermes' 0.20 contract |
| `empty_accessibility_tree` | `get_window_state` has no elements / `degraded` / `total_elements: 0` |
| `wrong_machine` | bridge hostname differs from the hostname recorded at pairing, or from `expected_host` (WSL/remote/gateway Hermes) |
| `unsupported_action` | action outside the enum, not advertised by the driver, or refused by Hermes |
| `approval_blocks_unattended` / `approval_blocked` | profile defaults to `single_query_mode: deny` / Hermes blocked an action |
| `no_display`, `hermes_missing`, `profile_not_paired`, `scope_violation`, `timeout_outcome_unknown`, ... | self-explanatory, each with a `next_action` |

On Linux/macOS none of the Windows checks can fire; the host reports `no_interactive_windows_session` as a warning, never an exception.

### 4.5 Direct mode and `replay(recipe)`
`direct_driver(scope)` opens a `DirectDriverSession`; `act(action, args, expect=...)` performs a Hermes-level action through typed calls (`protocol.py`, version `rebuild-studio.cua-driver-protocol/1`).
Rejected **before sending**: actions outside the enum, outside `scope.allowed_actions`, not advertised by the live driver, hard-blocked combos/typed patterns, bad arguments, `max_steps` exceeded, targets outside the title/process scope.
A mutating call whose transport fails or times out is recorded as unknown (`postcondition_ok: null`), never replayed, the session restarts, and a fresh capture is required.
`replay(recipe)`: recipe = `{schema: "rebuild-studio.hermes-recipe/1", protocol, name, target, steps:[{action, args?, locator?{role,label,label_contains,nth|x,y}, expect:[...], settle_s?, expect_timeout_s?}]}`;
locators are resolved against a fresh UI tree before each input (ambiguous or missing = fail with nothing sent); every mutating step must declare `expect` (`element_present|element_absent|tree_contains|window_title_contains|screenshot_changed|screenshot_sha|effect_confirmed`)
unless the recipe sets `allow_unchecked`; the first failed postcondition stops the run and no input is re-sent. Result: `extra.model_calls == 0`, Hermes is never launched.

## 5. Setup path when Hermes is absent
1. Run `GET /hermes/status` (or `HermesBridge().status()`): `hermes_missing` carries the exact command.
2. Install it yourself: Windows PowerShell `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`; Linux/macOS/WSL2 `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`. Rebuild Studio never runs these.
3. `hermes setup` (creates the profile and picks the model provider/account; keys stay in the profile), then `hermes computer-use install` and `hermes computer-use doctor`.
4. `POST /hermes/pair {profile_path?}`; then `POST /hermes/register_mcp {"dry_run": true}`, review the diff, repeat with `false`; verify with `hermes mcp list` / `hermes mcp test rebuild_studio`.
5. For unattended agent tasks decide, in your own profile, between `approvals.single_query_mode: approve` and `computer_use.permission_mode: bounded` + manifest (2.5). Without either, only captures work.
Without Hermes, `cua_driver_direct` (recipes) still works if cua-driver is installed: it needs no model and no Hermes profile.

## 6. Limitations found while reading the source
- **stream-json truncates tool output at 5000 chars** and Python-`str()`s multimodal captures, so a capture's `screenshot_path` is usually not recoverable from the stream. The bridge reads it when present
  (text-only branch) and otherwise assigns new `computer_use_*.png` files from the profile's image cache to captures in order (`screenshot_source: "cache_scan_ordered"`, approximate; the cache keeps only the newest 20 and paths are only read inside the profile dir).
  Post-action shas on agent records may be `post_screenshot_inferred` from the next capture.
- Agent-mode scope enforcement is **kill-on-observe**: `tool_use` is emitted as the tool starts, so the violating action may already be executing. Hard enforcement requires cua-driver bounded mode with a reviewed capability manifest in the profile.
  The bridge also limits `--max-turns`, `--run-budget`, `-t computer_use`.
- The capability-manifest schema is not in the Hermes repo (cua.ai docs only); the bridge neither writes nor validates one.
- `--max-turns` counts tool iterations per turn, not strictly desktop actions; the stream-derived action count is what `max_steps` checks.
- Hermes has no machine-identity endpoint, so `wrong_machine` rests on the hostname recorded at pairing (and an optional declared `expected_host`).

## 7. Windows-only gates (all UNTESTED on the Linux build host)
Run on a Windows 10/11 machine with an interactive logged-in desktop, the repo checked out and `pip install -e controller` done. Each gate is pass/fail; record outputs in CHECKPOINT.

| Gate | What it proves | Commands (PowerShell) | Expected |
|---|---|---|---|
| G1 Install + contract | Hermes and the pinned driver install natively and meet the contract | `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`; `hermes --version`; `hermes setup`; `hermes computer-use install`; `hermes computer-use status`; `hermes computer-use doctor --json`; `& (Get-Command cua-driver).Source manifest` | version line `Hermes Agent v... (...)`; driver `>= 0.20.0`; manifest has `mcp_invocation` and the `mcp/serve/stop` args; doctor `overall: ok` |
| G2 Detect + pair + register | profile discovery on `%LOCALAPPDATA%\hermes`, config merge on a real (possibly BOM/CRLF) config | `python -c "import json; from rebuild_controller.hermes import HermesBridge as B; b=B(); print(json.dumps(b.detect_installation(),indent=1)); print(b.pair()); r=b.register_mcp(True); print(r['diff'])"`; then `b.register_mcp(False)`; `hermes mcp list`; `hermes mcp test rebuild_studio` | install detected with path/version/profile; pairing written; diff is additions only; backup file exists; second `register_mcp(False)` reports `changed: false`; `hermes mcp test` connects |
| G3 Session probes | `ProcessIdToSessionId`, window station, `OpenInputDesktop`, elevation probe via ctypes | Normal desktop: `python -c "import json;from rebuild_controller.hermes import HermesBridge as B;print(json.dumps(B().status()['session'],indent=1))"`. Session 0: run the same over `ssh` into the box. Locked: press Win+L, run via a scheduled task or `psexec -i`. UIPI: `$p=Start-Process notepad -Verb RunAs -PassThru`, then `B().status(target_pid=$p.Id)` from a non-elevated shell | `interactive: true, session_id>0`; over SSH `session_0`; locked `desktop_locked`; elevated notepad `uipi_elevated_target`; none of the three on the clean desktop |
| G4 Direct driver | typed MCP calls work against the real cua-driver; protocol shapes match the recorded ones | `python -c "from rebuild_controller.hermes import *; b=HermesBridge(); import json; print(json.dumps(b.status(probe_tools=True)['capabilities'],indent=1))"`; start Notepad; replay a recipe: `python -c "import json;from rebuild_controller.hermes import *;print(json.dumps(HermesBridge().replay(Recipe.from_dict(json.load(open('notepad.json')))).to_dict(),indent=1))"` (author `notepad.json` per 4.5; Notepad's UIA role/label names vary by Windows build) | `live_tools` includes `get_window_state click type_text list_windows`; replay `completed`, `postcondition_ok: true`, `model_calls: 0`; diff the real `list_windows`/`get_window_state` JSON against `test_hermes.transcript()` and fix `protocol.py` if shapes differ |
| G5 Agent session | a model-driven session records actions and evidence against a real profile | In the profile set `approvals.single_query_mode: approve` (or bounded + manifest). `python -c "from rebuild_controller.hermes import *;r=HermesBridge().run_task(HermesTask(goal='Capture the Notepad window and tell me its title',target_window_title='Notepad',allowed_actions=('capture',),max_steps=5));print(r.status,r.records,r.diagnostics)"` | `completed`; records labelled `hermes_agent_session`; PNGs under `%LOCALAPPDATA%\hermes\cache\images\`; screenshot shas resolved (or flagged `cache_scan_ordered`) |
| G6 Stream shape | real `--format stream-json` output matches `StreamParser` | `hermes -p default chat -Q --format stream-json --query-file p.md --source tool -t computer_use --max-turns 3 > stream.jsonl` with a prompt asking for a capture; inspect `stream.jsonl` | `system/init`, `tool_use`/`tool_result` with `name: computer_use`, one `result`; check what a capture's `output` looks like (truncated repr vs JSON) and whether `tool_call_id` is present |
| G7 Process tree on Windows | cancel / disconnect kills Hermes **and** cua-driver, no orphans | start a long task, cancel it via job cancel and via `HermesBridge.cancel(task_id)`; `Get-Process hermes,python,cua-driver*` before/after | all gone within ~10 s (`taskkill /F /T`, `CREATE_NEW_PROCESS_GROUP`); also confirm a `hermes.cmd`/`.exe` shim launches under `Popen` without a shell |
| G8 Scope enforcement | violation kill and direct-mode pre-send rejection against real windows | agent task with `allowed_actions=('capture',)` on a prompt that asks for a click; direct session with `Scope(process_names=('notepad.exe',))` while Calculator is frontmost | agent: `scope_violation`, process gone; direct: `ScopeViolation`, driver log shows no input call |
| G9 No egress | direct mode is silent on the network | run G4 with `CUA_DRIVER_RS_TELEMETRY_ENABLED` unset in the parent; watch `Get-NetTCPConnection -OwningProcess (Get-Process cua-driver).Id` and Resource Monitor during the run | no connections from cua-driver or python; driver env dump shows `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` |

## 8. What the tests prove (and do not)
`tests/test_hermes.py` (36 tests) drives a fake `hermes` (a Python script on a temporary `PATH` that replays JSONL scenarios in the stream-json shape and records its argv/env) and a fake cua-driver (answers `manifest`,
`--version`, and stdio JSON-RPC `initialize`/`tools/list`/`tools/call` from a transcript). Both transcripts are **hand-built from the source contract above, not captured from a live driver**; shapes marked in 2.7 come straight
from the parsing code Hermes uses. They prove the bridge's own logic: detection/missing, pairing, config merge (dry-run, diff, backup, idempotence, comments, CRLF, shape errors), diagnostics (Windows branches through an injected probe),
action records and evidence, scope enforcement, cancel/disconnect/timeout/`StageContext` cancel killing the process tree, wrong-host refusal, pre-send rejection, replay with zero model calls, redaction, no credentials in tasks/env/driver env,
and no network (sockets are patched to fail during direct-mode tests and the package is statically checked for network imports). They do **not** prove that real Hermes/cua-driver on Windows behave as the source says: that is G1-G9.
