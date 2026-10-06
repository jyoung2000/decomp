# Parity report — dotnetapp

Generated 2026-10-06T12:16:49.107Z. Target: rust / exe.

**Full parity:** YES  — features: 8 total, 8 verified, 0 partial, 0 failed, 0 untested, 0 stale.
_feature counts are semantic features, not files/functions; undiscovered scope is not counted_

## Candidate r2 `cand_01a11124a9daabb70652b3`
build hash `2d9bade3618999b5`, build built, verification **verified**, last known good: True

## Features

| Feature | Origin | Critical | Implementation | Verification |
|---|---|---|---|---|
| quit immediately: defaults are saved to the state path on exit | user |  | runnable | verified |
| main -> notes -> add two notes, list, back, quit; state JSON byte-exact | user |  | runnable | verified |
| main -> settings -> set name, toggle theme, back, quit | user |  | runnable | verified |
| enter/leave both submenus, unknown choices, empty note/name rejected, delete of missing note | user |  | runnable | verified |
| pre-existing state is loaded (greeting shows it), a note is deleted and persisted, second run sees result | user |  | runnable | verified |
| stdin ends mid-menu: app saves and exits 0 | user |  | runnable | verified |
| malformed JSON: friendly message on stderr, exit 4, file untouched | user |  | runnable | verified |
| wrong argument count: usage on stderr, exit 2 | user |  | runnable | verified |

## Comparisons

| Feature | Channel | Rule | Verdict |
|---|---|---|---|
| dotnetapp.state_save | exit_code | exact | pass |
| dotnetapp.state_save | stdout | normalize:crlf | pass |
| dotnetapp.state_save | stderr | normalize:crlf | pass |
| dotnetapp.state_save | files | sha256:exact | pass |
| dotnetapp.menu_notes | exit_code | exact | pass |
| dotnetapp.menu_notes | stdout | normalize:crlf | pass |
| dotnetapp.menu_notes | stderr | normalize:crlf | pass |
| dotnetapp.menu_notes | files | sha256:exact | pass |
| dotnetapp.menu_settings | exit_code | exact | pass |
| dotnetapp.menu_settings | stdout | normalize:crlf | pass |
| dotnetapp.menu_settings | stderr | normalize:crlf | pass |
| dotnetapp.menu_settings | files | sha256:exact | pass |
| dotnetapp.menu_navigation | exit_code | exact | pass |
| dotnetapp.menu_navigation | stdout | normalize:crlf | pass |
| dotnetapp.menu_navigation | stderr | normalize:crlf | pass |
| dotnetapp.menu_navigation | files | sha256:exact | pass |
| dotnetapp.state_load | exit_code | exact | pass |
| dotnetapp.state_load | stdout | normalize:crlf | pass |
| dotnetapp.state_load | stderr | normalize:crlf | pass |
| dotnetapp.state_load | exit_code | exact | pass |
| dotnetapp.state_load | stdout | normalize:crlf | pass |
| dotnetapp.state_load | stderr | normalize:crlf | pass |
| dotnetapp.state_load | files | sha256:exact | pass |
| dotnetapp.menu_eof | exit_code | exact | pass |
| dotnetapp.menu_eof | stdout | normalize:crlf | pass |
| dotnetapp.menu_eof | stderr | normalize:crlf | pass |
| dotnetapp.menu_eof | files | sha256:exact | pass |
| dotnetapp.error_corrupt_state | exit_code | exact | pass |
| dotnetapp.error_corrupt_state | stdout | normalize:crlf | pass |
| dotnetapp.error_corrupt_state | stderr | normalize:crlf | pass |
| dotnetapp.error_corrupt_state | files | sha256:exact | pass |
| dotnetapp.error_corrupt_state | exit_code | exact | pass |
| dotnetapp.error_corrupt_state | stdout | normalize:crlf | pass |
| dotnetapp.error_corrupt_state | stderr | normalize:crlf | pass |
| dotnetapp.error_corrupt_state | files | sha256:exact | pass |
| dotnetapp.error_usage | exit_code | exact | pass |
| dotnetapp.error_usage | stdout | normalize:crlf | pass |
| dotnetapp.error_usage | stderr | normalize:crlf | pass |
| dotnetapp.error_usage | exit_code | exact | pass |
| dotnetapp.error_usage | stdout | normalize:crlf | pass |
| dotnetapp.error_usage | stderr | normalize:crlf | pass |
| dotnetapp.error_usage | files | sha256:exact | pass |

Environment: Linux 6.18.44-fc-v70 x86_64, wine=True, host_certifies_windows=False

## AI usage
- no AI calls were made for this case

## Reproduction

```
rebuildctl rebuild --source "/home/user/decomp/fixtures/dotnetapp/original" --output "/tmp/claude-0/-home-user-decomp/c456138f-a927-5bb9-bf2d-fa1845ce6ed1/scratchpad/dotnet-demo/out" --language rust --type exe
```
