# webapp → HTML/CSS/JS + PWA (deterministic port, no AI)

Pipeline run on 2026-10-06 (Linux host) over `fixtures/webapp/original` with **execute original = on** (web kind) and four declared scenarios.
- Recovery: JS/web backend inspected package/manifest/service worker, extracted the site; profile `web`.
- Baseline: captured from the original site served on a loopback port with the Playwright harness (DOM text, localStorage/hash, service-worker/manifest state, offline reload outcome, 1280×800 screenshot, console errors) and frozen by the verifier.
- Reconstruction: deterministic port of the recovered site (no model involved); web builder ran static PWA checks (manifest name/start_url/display/icons, SW registration).
- Verification: 4/4 scenario features verified across channels `dom`, `storage`, `offline`, `screenshot` (rule `pixel:exact`, 0 differing pixels) and `stderr` (console errors); the static hypothesis "Offline/PWA behaviour" stayed **untested** because no scenario targeted it — reported as such, not counted as parity.
- Independent check: `fixtures/webapp/harness` `npm run check -- --site <dist>` reported exact hash matches for all recorded screens and scenario values.
Evidence copies in `evidence/`. Caveats: screenshots are exact only on the same host/fonts/Chromium build (cross-machine comparisons need a declared tolerance, which the comparator supports and labels "approximate").
