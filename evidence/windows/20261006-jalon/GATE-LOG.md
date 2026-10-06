# Interactive Windows gate log — 2026-10-06, host "jalon"

Machine: Windows 11 Home 10.0.26200 x64, interactive desktop (owner account; admin-capable, installs were per-user and needed no elevation),
WebView2 Runtime 154.0.4258.53, two monitors (2560x1440 primary, 1920x1080 secondary). Not a clean machine: developer tools are installed.
Driven by Claude Code through Windows UI Automation (real mouse clicks / double-clicks on the real desktop), observed by screenshot.

## Run 1 — build d32eb7a (installer sha256 9a10a4fada7073e93c1db7c346ad454fc601bfc93ac23d6cfd4be210c57987e1, UNSIGNED)

| Step | Result | Evidence |
|------|--------|----------|
| Build on Windows (`Build-RebuildStudio.ps1`, PowerShell 5.1) | FAIL then fixed: `[char].Trim` (array unrolling) and stripped `"` in `python -c` under PS 5.1; after fixes UI + PyInstaller sidecar + NSIS + portable built; sidecar smoke `serve --port 0` wrote controller.json and answered /health | build logs (scratch), commit d32eb7a |
| GUI install from a folder with spaces | PASS: Welcome → Install location (default `%LOCALAPPDATA%\Rebuild Studio`) → progress → finish; no UAC prompt | screenshots during session |
| Finish page offers desktop shortcut | PASS: "Create desktop shortcut" checkbox, ticked by default; "Run Rebuild Studio" also offered | UIA tree of finish page |
| Shortcuts / app entry | PASS: `Desktop\Rebuild Studio.lnk`, `Start Menu\Programs\Rebuild Studio.lnk`, HKCU Uninstall key with DisplayIcon/InstallLocation; desktop .lnk `System.AppUserModel.ID` = `io.rebuildstudio.desktop` | reg query + Shell property read |
| Double-click desktop icon → full UI, no console | PARTIAL: UI window opened, no console/terminal/dev server; processes: rebuild-studio.exe + rebuild-controller.exe (PyInstaller onefile bootloader + child). Controller ready ≈5.3 s after spawn (cold, first run). **UI showed "Disconnected"** | screenshot; controller.log |
| Root cause | Controller answered the CORS preflight with 401 (token check ran first) and sent no `Access-Control-Allow-Origin`; the packaged webview origin `http://tauri.localhost` is cross-origin to `127.0.0.1:<port>`. Never seen before because UI e2e ran via the Vite dev server. Fixed in server.py + tests (`test_packaged_ui_origin_gets_cors`, `test_lookalike_origins_are_refused`) | |
| Normal close (window X) | PASS: all rebuild-* processes exited within 4 s; controller.json removed | tasklist |
