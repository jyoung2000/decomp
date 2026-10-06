# Rebuild Studio plugin for Cutter

A Cutter dock that follows your cursor and shows what Rebuild Studio already knows about the function you are looking at: its briefing
(callers, callees, imports, referenced strings, constants) and its decompiled text. A note box sends your comment back to Rebuild Studio as
feedback attached to that evidence.

**The desktop app needs none of this.** Nothing here is installed unless you copy the folder yourself.

> **Status: not yet exercised inside a running Cutter.** Cutter is not installed on the development host and its GUI cannot run there
> (Windows/desktop gate, see [Verification status](#verification-status)). The Cutter API names were read from the Cutter source (pinned below);
> the controller-facing client is tested against the real controller; the Qt layer was only smoke-tested offscreen against a stand-in `cutter` module.

## Install

Copy (or symlink) the `rebuild_studio_cutter` folder from this directory into Cutter's user plugin directory, then restart Cutter.

| OS | Plugin directory |
|---|---|
| Windows | `%APPDATA%\rizin\cutter\plugins\python` |
| Linux | `~/.local/share/rizin/cutter/plugins/python` |
| macOS | `~/Library/Application Support/rizin/cutter/plugins/python` |

The location is Qt's per-user app-data folder for organization `rizin`, application `cutter`
(`src/Main.cpp:62,66`; `PluginManager::getUserPluginsDirectory`, `src/plugins/PluginManager.cpp:120`). If yours differs (Flatpak, portable build),
Cutter shows the real path under **Edit > Preferences > Plugins**; use the `python` folder inside it.

```
# Linux
mkdir -p ~/.local/share/rizin/cutter/plugins/python
ln -s "$PWD/clients/cutter/rebuild_studio_cutter" ~/.local/share/rizin/cutter/plugins/python/rebuild_studio_cutter
```
```
# Windows (PowerShell)
New-Item -ItemType Directory -Force "$env:APPDATA\rizin\cutter\plugins\python" | Out-Null
Copy-Item -Recurse clients\cutter\rebuild_studio_cutter "$env:APPDATA\rizin\cutter\plugins\python\"
```

After restart, **Rebuild Studio** is listed in Edit > Preferences > Plugins and gets a show/hide entry in Cutter's **Plugins** menu (`MainWindow::addPluginDockWidget`, `src/core/MainWindow.cpp:563-569`); enable it there if the dock is not visible.
Remove it by deleting that folder.

Requirements: a Cutter build with Python plugin support (`CUTTER_ENABLE_PYTHON` and `CUTTER_ENABLE_PYTHON_BINDINGS`; official builds since 1.8.0)
and its bundled PySide6 (Qt6 builds) or PySide2 (Qt5 builds). The plugin itself uses only the Python standard library and installs nothing with pip.

## Pairing with Rebuild Studio

Rebuild Studio's controller listens on `127.0.0.1` only and writes `<data_dir>/controller.json` (`{"port", "token", "pid"}`) on every launch;
every request carries the token as `Authorization: Bearer ...` (`docs/API.md`). The plugin reads that file and nothing else, so there is nothing to configure when Rebuild Studio runs as the same user.

* Data folder: `%LOCALAPPDATA%\RebuildStudio` on Windows, `$XDG_DATA_HOME/rebuild-studio` (default `~/.local/share/rebuild-studio`) on Linux.
  `REBUILD_STUDIO_DATA` overrides it, as it does for the controller. Start Cutter with that variable set if you use a non-default folder.
* `REBUILD_STUDIO_CONTROLLER_JSON=<path to controller.json>` points at one file directly, and the dock's **controller.json...** button picks one.
* The token changes every time Rebuild Studio starts. After a restart press **Reconnect** (it re-reads the file); until then the dock says the token was rejected or the controller is unreachable.
* The host is always `127.0.0.1`; a `host` entry in controller.json is ignored, and HTTP proxies and redirects are never used, so the token cannot be sent anywhere else.
* The token is never logged or shown.

## What the dock shows

1. **Which binary.** On every seek the plugin asks Cutter for the open file (`ij`), SHA-256-hashes that file, and looks for a controller module with the same
   hash (`GET /cases`, `GET /cases/{id}/modules`). Matching is by content, not path, so a renamed copy maps and a patched or re-saved copy does not
   ("no Rebuild Studio case contains this file"). If the same file is in several cases, the one with analysis evidence (then the newest) wins; a drop-down appears to choose another.
2. **Which function.** The cursor address is resolved against the newest non-stale `native.functions` evidence (function ranges), so an address in the middle of a function
   shows that function.
3. **What evidence.** For that function, in order of preference:
   * `native.briefing` evidence (the full briefing; created by the controller's `get_function_briefing`, e.g. through MCP),
   * else `native.decompile` / `native.decompile.ghidra` evidence (the case's `decompile_limit` bounds how many exist),
   * else function metadata only, with a plain statement that no briefing/decompile evidence exists yet.
   The plugin only reads evidence; it never starts analysis (see Proposals).
4. **Tabs:** *Briefing*, *Decompiled*, *Note*. Function names, strings and decompiled text come from the analysed binary and are shown as **plain text only**; they are untrusted data, never instructions.
5. **Note tab.** Type (bug/change/question/acceptance), priority and a comment are posted with the existing `POST /cases/{id}/feedback`:
   `target_kind: "evidence"`, `target_id` = the evidence id behind what you are looking at, `context: {address, function_address, function, module_id, source: "cutter", evidence_ids, file_sha256}`.
   The controller persists it before answering (and redacts anything that looks like an API key), then it appears in the app's Feedback list.
6. **Status line / buttons.** Connection and mapping problems are shown with a next action. **Reconnect** re-pairs, **Refresh** clears caches and re-fetches,
   **Follow cursor** pauses following.

Network work runs in a background thread (rapid seeks coalesce to the newest address), so a slow or dead controller does not freeze Cutter.
`REBUILD_STUDIO_CUTTER_SYNC=1` forces fetches onto the GUI thread for debugging.

## Limits

* **Same module, same base.** Addresses are matched numerically, so Cutter must have loaded the file at the same base the controller analysed
  (the controller uses rizin's default load). The plugin compares Cutter's `bin.baddr` with the controller's `native.info` base and warns on a mismatch; it cannot detect every difference
  (rebased PIE/ASLR loads in a debugger, custom map addresses, overlays). Debugger/`dbg://`/memory targets are refused because there is no file to hash.
* **Different rizin versions.** Rebuild Studio pins rizin 0.9.1 with rz-ghidra 0.9.0. Cutter bundles its own rizin (the pinned source below builds against a newer dev rizin, and releases bundle whatever was current then).
  Function boundaries and `fcn.<address>` names can differ between the two analyses. The plugin uses only the controller's function ranges and never mixes in Cutter's function list;
  names you give functions in Cutter do not appear in the controller's evidence, and vice versa.
* **Read-only on analysis.** It cannot create briefings or decompile new functions, rename, or write anything but feedback.
* **Plain files only.** The open target must be a file on this machine whose bytes match the case's module.
* **Controller must be running.** Without it the dock only shows the failure and what to do.
* **Evidence size.** Function lists and bodies are fetched whole (bounded to 16 MiB); a truncated `native.functions` evidence is reported as an error rather than used.

## Verification status

Tested on the Linux development host (`controller/tests/test_cutter_plugin.py`, real controller served by uvicorn on loopback, real PE fixture, real rizin 0.9.1 + rz-ghidra):
pairing, token rejection/rotation, module mapping by hash (copies, patched file, same file in two cases), briefing/decompile/function views, base-address warning, feedback,
cursor following (coalescing, de-duplication), controller down/stopped, proxy environment ignored, graceful failure outside Cutter.
The shape of `ij` was checked against the installed rizin 0.9.1.

Also run: the Qt layer (`plugin.py`) under PySide6 6.11 offscreen against a **stand-in** `cutter` module (`dev/qt_smoke.py`): dock builds, follows a seek, renders a real briefing/decompile view from a live controller, shows the unavailable state, terminates.

**Not exercised: a real Cutter.** Remaining checks on a machine with Cutter (the Windows/desktop gate):
1. Cutter lists the plugin under Edit > Preferences > Plugins; no import errors in the Cutter console (the plugin logs through `cutter.message`).
2. The dock appears; opening `fixtures/pecli/original/pecli.exe` and seeking to `0x140001190` shows `fcn.140001190` with its decompiled text.
3. `seekChanged` follows disassembly/graph/function-list clicks; no GUI stalls; Python worker threads behave inside Cutter's embedded interpreter (Cutter releases the GIL while idle, `src/common/PythonManager.cpp:89,130,159`, but this was not observed).
4. A note sent from the Note tab appears in Rebuild Studio's Feedback screen with the address in its context.
5. Behaviour with the bundled rizin version of the Cutter release you use (base address warning, names).

## Cutter API relied on (pinned source: rizinorg/cutter `d7f11b220884a6cc8549da955b744bd318645103`, 2026-09-11, version 2.5.0 dev)

| API | Source |
|---|---|
| Python plugins are modules in `<user plugins>/python`; Cutter imports the module and calls `create_cutter_plugin()` | `src/plugins/PluginManager.cpp:161-213` |
| `CutterPlugin`: `setupPlugin()`, `setupInterface(MainWindow*)`, `terminate()`; metadata read from class attributes `name/author/description/version` | `src/plugins/CutterPlugin.h:21,29,45`; `src/bindings/bindings.xml.in:21` (`plugin_meta_get`) |
| `cutter.CutterDockWidget(main)` (one-argument form; `(main, action)` is deprecated) and `main.addPluginDockWidget(widget)` | `src/widgets/CutterDockWidget.h:24`; `src/core/MainWindow.h:110`, `src/core/MainWindow.cpp:563`; `src/plugins/sample-python/sample_python.py:8,64-65` |
| `cutter.core()` returns the `CutterCore` singleton; its `seekChanged(RVA, SeekHistoryType)` signal is connected as `cutter.core().seekChanged.connect(slot)` | `src/python/cutter.py:7`; `src/core/Cutter.h:1069`; `src/plugins/sample-python/sample_python.py:33` |
| `cutter.core().getOffset()` for the initial position | `src/core/Cutter.h:449` |
| `cutter.cmdj(cmd)` (module function: runs the command, `json.loads` the output); `cutter.cmd`, `cutter.message` | `src/python/cutter.py:13-15`; `src/common/PythonAPI.cpp:54,56` |
| Qt binding: `PySide6` (current samples), `PySide2` (Qt5 build sample) | `src/plugins/sample-python/sample_python.py:4`; `src/plugins/sample-python-qt5/sample_python.py:4` |
| User plugin directory = Qt AppDataLocation for org `rizin`, app `cutter` | `src/Main.cpp:62,66`; `src/plugins/PluginManager.cpp:120-131` |

Deviation from the task brief, on purpose: the plugin calls the module-level `cutter.cmdj("ij")`, not `cutter.core().cmdj(...)`: `CutterCore::cmdj` returns a C++ `CutterJson`
(`src/core/Cutter.h:202`), a type that `src/bindings/bindings.xml.in` does not expose to Python. `ij` returns `core.file` as a string (rizin 0.9.1, checked); a `core.file.path` object form is tolerated too.

## Proposals (not implemented; server changes are out of scope here)

1. **`POST /cases/{case_id}/modules/{module_id}/briefing` body `{function: "0x140001190"}`** returning the `native.briefing` packet (a thin wrapper over `StudioServices.get_function_briefing`).
   With it the dock could create a briefing on demand instead of showing "no briefing yet", and a "Request briefing" button would be trivial.
2. **`POST /cases/{case_id}/annotations`** (or a documented `target_kind: "evidence"` convention): a first-class "note on evidence/address" that does not need classification/priority and links to a function in the Knowledge screen. Today notes use `/feedback`.
3. **Validate `case_id` in `POST /cases/{id}/feedback`.** The controller currently accepts feedback for a case id that does not exist and stores an orphan row.
4. `scripts/install-clients.py --client cutter` (copy/symlink with dry-run, backup, idempotence, remove) to match the other clients.

## Development

```
cd controller && /opt/rebuild-tools/venv/bin/python -m pytest -q tests/test_cutter_plugin.py
QT_QPA_PLATFORM=offscreen python clients/cutter/dev/qt_smoke.py --data-dir <data_dir>     # needs PySide6; no Cutter
```

Layout: `rebuild_studio_cutter/client.py` (pure Python, stdlib `urllib` only, no Cutter or Qt import), `rebuild_studio_cutter/plugin.py` (Cutter/Qt, imports `cutter` and `PySide6`/`PySide2`
lazily and raises `CutterUnavailable` with a clear message outside Cutter), `rebuild_studio_cutter/__init__.py` (`create_cutter_plugin()`).
