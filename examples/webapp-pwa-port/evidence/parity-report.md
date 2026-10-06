# Parity report — webapp

Generated 2026-10-06T11:52:08.633Z. Target: web / pwa.

**Full parity:** NO  — features: 5 total, 4 verified, 0 partial, 0 failed, 1 untested, 0 stale.
_feature counts are semantic features, not files/functions; undiscovered scope is not counted_

## Candidate r1 `cand_01a1110e68a802f4e4194c`
build hash `5cdf1e0f7918ce24`, build built, verification **verified**, last known good: True

## Features

| Feature | Origin | Critical | Implementation | Verification |
|---|---|---|---|---|
| Notes view renders | runtime | yes | runnable | verified |
| Add notes | runtime |  | runnable | verified |
| About route | runtime |  | runnable | verified |
| Offline reload | runtime |  | runnable | verified |
| Offline/PWA behaviour | static |  | runnable | untested |

## Comparisons

| Feature | Channel | Rule | Verdict |
|---|---|---|---|
| webapp.render_notes | dom | innerText:exact(per step) | pass |
| webapp.render_notes | storage | localStorage+hash:exact | pass |
| webapp.render_notes | offline | sw_registered+manifest_present+reload_outcome:exact | pass |
| webapp.render_notes | screenshot | pixel:exact | pass |
| webapp.render_notes | stderr | console_errors:count<=baseline | pass |
| webapp.add_note | dom | innerText:exact(per step) | pass |
| webapp.add_note | storage | localStorage+hash:exact | pass |
| webapp.add_note | offline | sw_registered+manifest_present+reload_outcome:exact | pass |
| webapp.add_note | screenshot | pixel:exact | pass |
| webapp.add_note | stderr | console_errors:count<=baseline | pass |
| webapp.route_about | dom | innerText:exact(per step) | pass |
| webapp.route_about | storage | localStorage+hash:exact | pass |
| webapp.route_about | offline | sw_registered+manifest_present+reload_outcome:exact | pass |
| webapp.route_about | screenshot | pixel:exact | pass |
| webapp.route_about | stderr | console_errors:count<=baseline | pass |
| webapp.offline | dom | innerText:exact(per step) | pass |
| webapp.offline | storage | localStorage+hash:exact | pass |
| webapp.offline | offline | sw_registered+manifest_present+reload_outcome:exact | pass |
| webapp.offline | screenshot | pixel:exact | pass |
| webapp.offline | stderr | console_errors:count<=baseline | pass |

Environment: Linux 6.18.44-fc-v70 x86_64, wine=True, host_certifies_windows=False

## Unresolved
- Offline/PWA behaviour: impl runnable, verify untested

## AI usage
- no AI calls were made for this case

## Reproduction

```
rebuildctl rebuild --source "/home/user/decomp/fixtures/webapp/original" --output "/tmp/pipe-web-ibp5p25b/out" --language web --type pwa
```
