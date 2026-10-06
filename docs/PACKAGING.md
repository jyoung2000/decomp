# Packaging and developer setup

How Rebuild Studio is built, packaged and installed on Windows, and how a developer works on it from Windows or Linux.
Windows release certification is **not** claimed anywhere in this repository: see `docs/WINDOWS_RELEASE_GATES.md`.

## What is produced

| Artifact | Made by | Notes |
|----------|---------|-------|
| `desktop/src-tauri/binaries/rebuild-controller-x86_64-pc-windows-msvc.exe` | `Build-RebuildStudio.ps1` step 3 (PyInstaller `--onefile`) | The Tauri sidecar (`bundle.externalBin`). Entry `scripts/windows/sidecar_entry.py`, which calls the same `main()` as `python -m rebuild_controller.cli.main`. |
| `rebuild-mcp.exe` (portable zip, `runtime\Scripts\`) | step 3, second PyInstaller build of `scripts/windows/mcp_entry.py` | Stdio MCP server for the optional client packages (`Install-Clients.ps1`). |
| `RebuildStudio-<ver>-x64-setup[-UNSIGNED].exe` | step 4, `cargo tauri build --bundles nsis` | Per-user NSIS installer (`installMode: currentUser`, WebView2 `downloadBootstrapper`, silent). |
| `RebuildStudio-<ver>-win-x64-portable[-UNSIGNED].zip` | step 5 | The unbundled app dir plus scripts, docs, clients and `SHA256SUMS.txt`; installed by `Install-RebuildStudio.ps1`. |
| `dist\sbom\*`, `dist\NOTICES.md`, `dist\BUILD-INFO.json`, `dist\SHA256SUMS.txt` | steps 6-7 | CycloneDX SBOMs, license data, build record. `dist\UNSIGNED.txt` exists unless `-SignCert` was used and verified. |

Output folders (`dist\`, `build\windows\`) get a `.gitignore` containing `*` from the build script, so no repository ignore edit is needed.
Nothing produced here is certified: unsigned artifacts carry `-UNSIGNED` in their names and in `BUILD-INFO.json`.

## Developer setup

### Windows 10/11 x64

| Need | Version | Install |
|------|---------|---------|
| Git | any recent | <https://git-scm.com/download/win> (Git Bash gives you `sh` for the stub helper below) |
| Node.js | 22 LTS (lock: 22.22.0), npm >= 10 | <https://nodejs.org/> |
| Python | 3.12 (3.11-3.13 accepted) | <https://www.python.org/downloads/windows/> (add to PATH, or `py -3.12`) |
| Rust | stable (the shell declares `rust-version = 1.90`) with target `x86_64-pc-windows-msvc` | <https://rustup.rs/> |
| MSVC build tools | Visual Studio 2022 Build Tools, workload "Desktop development with C++" + Windows 10/11 SDK | <https://visualstudio.microsoft.com/downloads/> |
| WebView2 Evergreen runtime | >= 110 | usually present on Windows 11; otherwise `Install-RebuildStudio.ps1` installs it, or <https://go.microsoft.com/fwlink/p/?LinkId=2124703> |
| tauri-cli | exactly 2.12.1 | `cargo install tauri-cli --version 2.12.1 --locked` |
| cargo-cyclonedx (optional) | 0.5.9 | `cargo install cargo-cyclonedx --version 0.5.9 --locked` (without it the build writes `cargo metadata` + `Cargo.lock` instead of a CycloneDX file) |

```powershell
git clone <repo> ; cd <repo>
.\scripts\windows\Build-RebuildStudio.ps1 -DryRun        # prints every step and command, changes nothing
.\scripts\windows\Build-RebuildStudio.ps1                # full clean build, unsigned (30-60 min first time)
.\scripts\windows\Doctor-RebuildStudio.ps1               # environment report
```

Inner loop (no packaging):

```powershell
cd controller; python -m venv .venv; .venv\Scripts\Activate.ps1; pip install -e ".[dev]"; python -m pytest -m "not e2e and not live" -q
cd ..\ui; npm ci; npm test; npm run dev                          # Vite on http://localhost:5173 (tauri.conf.json devUrl)
cd ..; sh desktop/scripts/ensure-sidecar-stub.sh x86_64-pc-windows-msvc   # Git Bash; tauri-build needs the externalBin file to exist
cd desktop\src-tauri; cargo tauri dev                            # debug build: falls back to `python -m rebuild_controller.cli.main serve`
```

For `cargo tauri dev` the shell needs a Python that can import `rebuild_controller`: activate the venv above, or set `REBUILD_STUDIO_PYTHON` (interpreter) and `REBUILD_STUDIO_CONTROLLER_DIR` (the `controller\` folder, used as `PYTHONPATH` and cwd).

### Linux (what the Linux host and `linux.yml` can do)

```sh
sudo apt-get install -y libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev patchelf   # Tauri 2 Linux deps
sh desktop/scripts/check.sh            # cargo check --locked (creates the dev stub sidecar; uses desktop/placeholder-dist when ui/dist is absent)
sh desktop/scripts/check.sh test       # cargo test --locked (24 unit tests; Windows-only tests are cfg'd out)
cd controller && pip install -e ".[dev]" && python -m pytest -m "not e2e and not live" -q
cd ui && npm ci && npm test && npm run build
# type-check the Windows-only code paths without Windows (needs `rustup target add x86_64-pc-windows-gnu` and a stub for that triple):
sh desktop/scripts/ensure-sidecar-stub.sh x86_64-pc-windows-gnu && (cd desktop/src-tauri && cargo check --locked --target x86_64-pc-windows-gnu --tests)
```

Linux can compile and unit-test the shell, run the controller and UI suites, and dry-run/parse the PowerShell scripts (`python scripts/windows/tests/parse_check.py`; with `pwsh` it also uses the real PowerShell parser and `Test-SetupDependencies.ps1` and the `-DryRun` modes run). It cannot build the NSIS installer, run the real sidecar `.exe`, exercise WebView2, Credential Manager, DPAPI, folder dialogs or Hermes: those are the Windows gates.

## How the sidecar is found

The shell (`desktop/src-tauri/src/controller.rs`) always launches the controller as `rebuild-controller serve` with `REBUILD_STUDIO_DATA` (and `REBUILD_STUDIO_TOOLS`, `REBUILD_STUDIO_INSTALL`) set, waits for `<data>\controller.json` to parse **and** `GET /health` to answer, injects `{baseUrl, token}` into the window and kills the whole process tree on exit (Windows: `taskkill /T /F` plus a kill-on-close job object).

| Mode | When | Which binary |
|------|------|--------------|
| **Installed / portable** | release builds, or debug builds when the sidecar file is a real binary | Tauri resolves `rebuild-controller.exe` **next to `rebuild-studio.exe`** (the bundler strips the target triple: NSIS puts both in the install folder; the portable zip does the same). It must be at least 64 KiB; anything smaller is treated as a dev stub. |
| **Dev (Python fallback)** | debug builds only, when the sidecar is missing or a stub; or `REBUILD_STUDIO_DEV_PYTHON=1` to force it | `python -m rebuild_controller.cli.main serve` (`REBUILD_STUDIO_PYTHON`, `REBUILD_STUDIO_CONTROLLER_DIR`) |
| **Release without a real sidecar** | never allowed | startup fails with an error naming `rebuild-controller`; `REBUILD_STUDIO_ALLOW_PYTHON_FALLBACK=1` overrides this for diagnostics only |
| **External** | `REBUILD_STUDIO_CONTROLLER_EXTERNAL=1` | attach to a controller you started yourself (`rebuildctl serve --data-dir <dir>`); the shell never kills it |

In the source tree the sidecar lives at `desktop/src-tauri/binaries/rebuild-controller-<target-triple>[.exe]` (git-ignored except `.gitkeep`). `tauri-build` refuses to compile when that file is missing, so `desktop/scripts/ensure-sidecar-stub.sh [triple]` creates a tiny stub for `cargo check/test/dev`. `Build-RebuildStudio.ps1` overwrites the stub with the real PyInstaller build and refuses to continue if the file is under 1 MB. `Doctor-RebuildStudio.ps1` fails an install whose `rebuild-controller.exe` is under 1 MB.

PyInstaller notes (`Build-RebuildStudio.ps1`, step 3):
- Two venvs under `build\windows\`: `venv-runtime` holds only the controller's dependencies (its `pip freeze` becomes `dist\sbom\python-freeze.txt` and the constraints file for the build venv, so the SBOM describes exactly what is bundled), `venv-build` adds PyInstaller and the SBOM tools from `scripts/windows/requirements-build.txt`.
- pip installs the controller's dependencies from a throw-away copy (`build\windows\controller-src`), never in-tree: a non-editable `pip install controller\` writes `controller\build\lib` and `*.egg-info` into the checkout (both are tracked in this repository today) and would dirty a clean clone.
- The `rebuild_controller` package is taken from the **source tree** (`--paths controller`), with its non-Python files added explicitly (`store/schema.sql`, `comparators/web_harness.mjs`): `controller/pyproject.toml` declares no package data, so a wheel/installed copy would miss them. Fixing that in the controller makes the explicit step redundant (it is harmless then).
- `--collect-all lief`, `--collect-submodules rebuild_controller|uvicorn|websockets` cover native/lazy imports (verified on Linux by freezing and serving `/health`; not yet on Windows). `mcp.cli` (needs `typer`) is excluded from `rebuild-mcp`.
- One-file mode unpacks to `%TEMP%\_MEI*` on every start (cold start plus Defender can be tens of seconds; the shell waits 90 s, `REBUILD_STUDIO_STARTUP_TIMEOUT_SECS`). If gate W15 shows this is too slow, switch to one-dir and ship the folder as a Tauri `resources` entry.
- Not bundled: Rizin, GDRE, ilspycmd, .NET, Node (downloaded per user by `Setup-Dependencies.ps1`), and the browser comparison harness (`controller/harness`, Playwright): that channel reports itself unavailable in a packaged build (gate W20).

## Install layout and scripts

Portable zip / `Install-RebuildStudio.ps1` layout (the script copies this to `%LOCALAPPDATA%\Programs\RebuildStudio`):

```
rebuild-studio.exe  rebuild-controller.exe  NOTICES.md  SHA256SUMS.txt  [UNSIGNED.txt]
runtime\Scripts\rebuild-mcp.exe
scripts\install-clients.py   scripts\windows\{_common,Doctor-RebuildStudio,Setup-Dependencies,Install-Clients,Remove-Clients,Install-RebuildStudio,Uninstall-RebuildStudio}.ps1
docs\{NOTICES.md,dependency-lock.json}     clients\**
```

- `Install-RebuildStudio.ps1` verifies every file against `SHA256SUMS.txt`, checks WebView2 (registry keys from the lock; if missing downloads Microsoft's bootstrapper from the documented URL, requires a valid Authenticode signature from "Microsoft Corporation", runs it silently; **never bundles the runtime**), swaps the install directory atomically (`.new` -> live, previous kept as `.previous`), creates `%LOCALAPPDATA%\RebuildStudio` (data dir), writes `<data>\install.json` (read by Doctor), a Start-menu shortcut and a per-user Apps & Features entry. `-DryRun` shows all of it.
- `Uninstall-RebuildStudio.ps1` removes the program, shortcut, Apps entry and `install.json`, and **keeps** `<data>` and the Credential Manager entries `RebuildStudio:*` unless `-RemoveUserData` (typed `DELETE` or `-Force`). `-RemoveTools` deletes only `<data>\tools`; `-RemoveClients` runs `Remove-Clients.ps1` first.
- The NSIS installer is an alternative per-user install (default folder `%LOCALAPPDATA%\Rebuild Studio`); it does not contain the scripts. Use one install method per machine.
- Data dir resolution is identical in the shell (`paths.rs`), the controller (`config.default_data_dir`) and the scripts (`_common.ps1`): `REBUILD_STUDIO_DATA`, else `%LOCALAPPDATA%\RebuildStudio` (POSIX: `$XDG_DATA_HOME/rebuild-studio`).

## `docs/dependency-lock.json`

Single source of truth for pinned external dependencies. `schema_version: 1`.

| Section | Used by | Meaning |
|---------|---------|---------|
| `tools.<name>` (`rizin`, `gdre`, `ilspycmd`, `dotnet-runtime`, `node`) | `Setup-Dependencies.ps1`, `Doctor-RebuildStudio.ps1` | `artifact.url` + `artifact.sha256` (archive hash), `layout.entry` + `layout.entry_sha256` (the binary after extraction), optional `layout.extra_files`, `install_dir`, `optional`. |
| `system_prerequisites.webview2_evergreen_bootstrapper` | `Install-RebuildStudio.ps1`, Doctor | Registry keys, minimum version, bootstrapper URL, required Authenticode subject (no hash: Microsoft re-publishes the file). |
| `system_prerequisites.vc_redist_x64` | Doctor | Detect only; nothing is bundled. |
| `build_toolchain` | `Build-RebuildStudio.ps1` (node major, python range, tauri-cli version), CI env, humans | Versions the build was validated with. |
| `sidecar_build` | documentation | Where the sidecar comes from. |

How `Setup-Dependencies.ps1` uses it, per tool: download over https only -> compare SHA-256 with `artifact.sha256` (a `null` hash means **refuse**, nothing is installed) -> extract into `tools\.staging` with zip-slip/size guards -> compare `layout.entry_sha256` -> rename the live folder to `tools\.previous\<dir>-<version>` and the staged folder into place (undone if the second rename fails). `-Rollback` swaps the previous version back. `-ComputeHash <tool>` downloads one artifact and prints its hash for you to cross-check and record. Doctor re-hashes installed entries and reports `mismatch` for anything that differs from the lock.

## Updating pins

1. **A downloaded tool** (rizin, gdre, ilspycmd, node, dotnet-runtime):
   1. Edit `version`, `source`, `artifact.name/url` in the lock; set `artifact.sha256`, `size_bytes`, `layout.entry_sha256` to `null`, `verify_required: true`.
   2. `.\scripts\windows\Setup-Dependencies.ps1 -ComputeHash <tool>`; cross-check the printed hash against the vendor's published value (release page, `SHASUMS256.txt`, `releases.json`) and record where you checked in `hash_provenance`.
   3. Install it into a scratch tools root (`-ToolsRoot $env:TEMP\tt`), then compute `layout.entry_sha256` (and `extra_files`) with `Get-FileHash` on the extracted binary and paste them.
   4. `Setup-Dependencies.ps1 -Tool <tool> -Force` then `Doctor-RebuildStudio.ps1 -Smoke`; update `docs/NOTICES.md` (version/license table), `docs/SUPPORT_MATRIX.md` and `docs/DECISIONS.md` if the choice changes; run gate W3.
2. **Build toolchain** (tauri-cli, PyInstaller, cargo-cyclonedx, pip-licenses, cyclonedx-bom): change `scripts/windows/requirements-build.txt` (Python tools) and the `TAURI_CLI_VERSION` / `CARGO_CYCLONEDX_VERSION` env in `.github/workflows/windows.yml`, then `docs/dependency-lock.json` -> `build_toolchain`. tauri-cli must keep matching the `tauri` crate minor series in `desktop/src-tauri/Cargo.toml`.
3. **Rust crates** (exact `=x.y.z` pins in `desktop/src-tauri/Cargo.toml`): edit, `cargo update -p <crate> --precise <ver>` or `cargo check` to refresh `Cargo.lock`, run `sh desktop/scripts/check.sh test`, the `--target x86_64-pc-windows-gnu --tests` check, update `build_toolchain.crates` in the lock, and re-run W6/W10 (Windows-only tests).
4. **npm packages**: edit `ui/package.json` (exact versions), `npm install`, commit `package-lock.json`; CI uses `npm ci`.
5. **Python runtime dependencies**: ranges live in `controller/pyproject.toml`; the build records the resolved set per build in `dist\sbom\python-freeze.txt`. To freeze versions for a release, commit a constraints file and pass it to the `venv-runtime` install (currently resolved fresh on each build).
6. **WebView2 / VC++ minimums**: `system_prerequisites.*.minimum_version` in the lock; Doctor and the install script read them.
7. After any change: `python scripts/windows/tests/parse_check.py`, `pwsh scripts/windows/tests/Test-SetupDependencies.ps1`, `pwsh scripts/windows/Build-RebuildStudio.ps1 -DryRun`, and the `linux.yml` jobs must stay green.

## CI

- `.github/workflows/linux.yml`: controller `pytest -m "not e2e and not live"` (Python 3.12 and 3.13; the two wine-based tests are deselected), UI `npm test` + `npm run build`, desktop `cargo check` + `cargo test` with the Tauri Linux apt packages, PowerShell parse/offline tests/dry runs, an advisory wine-oracle job.
- `.github/workflows/windows.yml`: Node 22, Rust stable, Python 3.12; UI + controller + Rust tests, `Build-RebuildStudio.ps1`, install/doctor/launch/uninstall of the portable zip, then uploads installer, portable zip, SBOM/notices and evidence. **Everything uploaded is unsigned** (artifact names say so) and nothing is certified. The workflow had not been executed when it was written.
- Validate workflow syntax locally: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/windows.yml'))"` (and `actionlint` if available).
