# webapp fixture (vanilla PWA + Electron-style package)

"Pocket Notes": `src/index.html`, `app.js`, `styles.css`, `manifest.webmanifest`, `sw.js`, `icon.svg`, and `src/electron/{main.js,package.json}`
(only used for the Electron package). No framework, no bundler.

* State: `localStorage["pocket-notes:v1"]` = `{"nextId":N,"notes":[{"id":1,"text":"..."}]}`.
* Routes by hash: `#/notes` (default, add/list/delete) and `#/about` (version, note count, cache names).
* Error state: any `setItem` failure (e.g. `QuotaExceededError`) shows `#error[role=alert]` "Storage is full. Your latest change was not saved.",
  keeps the previous state, and clears on the next successful save. `?simulate-quota=1` forces the same path without monkey-patching.
* `sw.js`: cache `notes-cache-v1` precached on install, `skipWaiting`, on activate deletes every older `notes-cache-*` cache and claims clients;
  fetch is cache-first with network fill and navigation fallback to `index.html`; answers `GET_VERSION` messages. App shows a banner on `controllerchange` after an upgrade.

## Build
`./build.sh`: `original/` = the six web files (deployable site). `original-electron/resources/app.asar` = web files + `main.js` + `package.json`
packed with `@electron/asar` **4.3.1** (pinned in `harness/package.json` + `package-lock.json`; sorted order, no mtimes -> deterministic, sha256 `bc0700ee...7174`).
`original-electron/ELECTRON_BINARY_MISSING.txt` is the placeholder for the absent Electron runtime. Manifests: `original.manifest.json`,
`original-electron.manifest.json`.

## Oracle (`expected/web_scenarios.json`, `expected/screens/*.png`)
`cd harness && npm test` (Playwright **1.56.1**, pre-installed Chromium 141.0.7390.37 via `PLAYWRIGHT_BROWSERS_PATH`, no `playwright install`) records,
against a local static server (`harness/server.mjs`):
* `original` (14 scenarios): initial load, add notes, persistence after reload, about route, back to notes, delete, quota (real `setItem` throw + recovery,
  and `?simulate-quota`), offline reload / offline about route / unknown navigation fallback / uncached asset failure, and SW cache upgrade v1 -> v2.
  Each has DOM text, nav state, error/banner state, all `localStorage` entries, and cache names where relevant.
* `electron_asar` (9 scenarios): asar contents + package.json, same functional scenarios loaded through `file://` (as Electron `loadFile`), no service worker.
* Screenshots at 1280x800 (notes with two items, about, quota error) for both targets in `expected/screens/`.
`npm test` runs the whole recording **twice** and fails if any scenario value differs (they match, and so do the PNG hashes on this host).
`npm run check [-- --site DIR]` replays the scenarios against another site build and diffs the DOM/localStorage/offline results (screenshot hashes are reported, not enforced).

Offline is simulated by making the test server drop connections *and* `context.setOffline(true)` (Playwright's flag alone does not cover SW fetches).

## Features (stable ids)
`webapp.render_notes`, `webapp.add_note`, `webapp.delete_note`, `webapp.localstorage_state`, `webapp.hash_routes`, `webapp.quota_error_state`,
`webapp.offline_reload`, `webapp.sw_cache_upgrade`, `webapp.screenshot_1280x800`, `webapp.electron_asar_payload` (all `linux_via_playwright_chromium`),
`webapp.electron_runtime` (`electron_runtime_only`: Electron binary missing, never launched).
Screenshots depend on host fonts/Chromium build: compare with a declared tolerance rather than by hash across machines.
