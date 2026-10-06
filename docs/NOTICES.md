# Third-party notices

Rebuild Studio is assembled from the components below. This file lists what is **shipped inside the installer / portable zip**
and what is **downloaded on the user's machine by `Setup-Dependencies.ps1`** (pinned in `docs/dependency-lock.json`).
The authoritative, version-exact inventory for each build is the SBOM produced by `scripts/windows/Build-RebuildStudio.ps1`
(`dist/sbom/*.cdx.json` and `dist/sbom/python-licenses.json`). Where this summary and the SBOM disagree, the SBOM wins.
The SBOM set is `ui.cdx.json` (npm, shipped packages only), `rebuild-studio.cdx.json` (Rust, resolved for the Windows target; when
`cargo-cyclonedx` is absent the build writes `cargo-metadata.json` + `Cargo.lock` instead), `controller.cdx.json` and
`python-licenses.json` (Python; the latter embeds each package's license text) and `python-freeze.txt` (the exact versions that
PyInstaller bundled). Rust and npm entries carry SPDX identifiers; their full license texts are available from the upstream
projects (a `cargo about`-style bundle is a release-owner task, tracked as gate W18 in `docs/WINDOWS_RELEASE_GATES.md`).

## Original application content is never redistributed

Rebuild Studio analyses software that **the user supplies**. Input binaries, extracted assets, decompiler output, recovered
source and generated rebuilds stay in the user's own data and output folders. They are never uploaded, bundled into an
installer, added to the knowledge store as redistributable content, or shipped as test fixtures. Test fixtures in this
repository are built from source written for this project (see `fixtures/`). No original game/application code, art or audio
from a third party is part of any release artifact.

## Shipped in the installer / portable zip

| Component | License | Notes |
|-----------|---------|-------|
| Tauri 2 (`tauri`, `tauri-build`, `tauri-utils`, `tauri-runtime`, `wry`, `tao`) | MIT OR Apache-2.0 | Desktop shell |
| `tauri-plugin-dialog`, `-shell`, `-opener`, `-window-state`, `-single-instance` | MIT OR Apache-2.0 | Official Tauri plugins |
| `serde`, `serde_json`, `libc`, `windows` (windows-rs) | MIT OR Apache-2.0 | Rust shell dependencies |
| Other transitive Rust crates | See `dist/sbom/rebuild-studio.cdx.json` | Predominantly MIT / Apache-2.0 / Unicode-3.0 / MPL-2.0 (`cargo about`-style review recommended before a public release) |
| React, React DOM, React Router | MIT | UI |
| `@tauri-apps/api` | MIT OR Apache-2.0 | UI to shell IPC |
| Other npm packages bundled by Vite | See `dist/sbom/ui.cdx.json` | Dev-only tooling (Vite, Vitest, Playwright, TypeScript) is not shipped |
| CPython runtime (inside the PyInstaller bundle) | PSF-2.0 | Python Software Foundation License |
| `rebuild-mcp.exe` (second PyInstaller build of the same Python packages) | as the controller rows | Only in the portable zip (`runtime\Scripts\`), used by the optional client packages |
| PyInstaller bootloader + runtime hooks | GPL-2.0-or-later with the PyInstaller bootloader exception (Apache-2.0 for hooks) | The exception permits distributing the frozen controller under any license; the controller is not GPL |
| `pydantic` | MIT | Controller |
| `fastapi`, `starlette` | MIT / BSD-3-Clause | Controller HTTP API |
| `uvicorn`, `websockets`, `httpx`, `httpcore`, `h11`, `anyio` | BSD-3-Clause | Controller transport |
| `mcp` (Model Context Protocol SDK) | MIT | MCP server |
| `rzpipe` | MIT | Talks to the Rizin subprocess; does not link Rizin |
| `lief` | Apache-2.0 | PE/ELF/Mach-O parsing |
| `pefile` | MIT | PE parsing |
| `pillow` | MIT-CMU (HPND) | Screenshot comparison |
| Other Python dependencies | See `dist/sbom/python-licenses.json` | Produced by `pip-licenses` |

## Downloaded on the user's machine (not part of the installer)

These are fetched from the upstream project's own release URL, hash-verified against `docs/dependency-lock.json`, and
installed under `%LOCALAPPDATA%\RebuildStudio\tools`. Rebuild Studio runs them as **separate processes**; none is linked into
or relicensed with Rebuild Studio.

| Component | Pinned version | License | Notes |
|-----------|----------------|---------|-------|
| Rizin | v0.9.1 | LGPL-3.0-only (some optional bundled libraries carry their own licenses; see the license files in the Rizin release) | Subprocess / pipe use only (dynamic-use rule of LGPL). Source: <https://github.com/rizinorg/rizin> tag `v0.9.1`. Users may substitute their own Rizin build under `tools\rizin`. |
| GDRE Tools (gdsdecomp) | v2.7.0 | MIT | Embeds Godot Engine (MIT). Source: <https://github.com/GDRETools/gdsdecomp> |
| ILSpy command line (`ilspycmd`) | 9.1.0.7988 | MIT | Includes ICSharpCode.Decompiler (MIT) and Mono.Cecil (MIT). NuGet package `ilspycmd` |
| .NET 8 runtime | 8.0.31 | MIT (runtime) | Side-by-side `tools\dotnet`; no machine-wide install |
| Node.js (optional, JS/Electron extraction) | 22.22.0 | MIT (plus bundled third-party licenses in `LICENSE`) | Only if the JS path is used |

## Not redistributed, required on the machine

| Component | License / terms | How it is obtained |
|-----------|-----------------|--------------------|
| Microsoft Edge WebView2 Runtime (Evergreen) | Microsoft Software License Terms | The installer's `downloadBootstrapper` mode / `Install-RebuildStudio.ps1` downloads Microsoft's bootstrapper and requires a valid Microsoft Authenticode signature. Not bundled. |
| Microsoft Visual C++ 2015-2022 Redistributable (x64) | Microsoft Software License Terms | Detected by `Doctor-RebuildStudio.ps1`; the user installs it from Microsoft. Not bundled. |
| NSIS (build-time only) | zlib/libpng-style license (installer stub), bzip2, CPL-1.0 (LZMA SDK parts) | Installer generator; its stub is embedded in the setup executable. |

## Not bundled in the packaged controller

The browser-channel comparator (`controller/harness`, Node + Playwright) and Chromium are **not** part of the frozen controller; that
channel reports itself unavailable in a packaged build until Node, Playwright and a browser are provisioned (see `docs/PACKAGING.md`).

## Unsigned builds

Artifacts produced by this repository's CI and by `Build-RebuildStudio.ps1` without `-SignCert` are **unsigned** and are named
`...-UNSIGNED...`. Windows SmartScreen will warn on first run. A code-signing certificate and timestamp server are a release-owner
decision (gate W17 in `docs/WINDOWS_RELEASE_GATES.md`).
