# Support matrix (updated 2026-10-06; "verified" = a fixture regression passed on the named host; Windows column = certified on Windows)

| Profile | Detection | Recovery backend | Pinned version | Linux host (this session) | Windows |
|---------|-----------|------------------|----------------|---------------------------|---------|
| Native PE x64 | pefile/LIEF (CLR header, subsystem, imports) | Rizin via rzpipe + rz-ghidra decompiler | rizin v0.9.1 (c3a90e92) source build; rz-ghidra v0.9.0 (999df7b8); static fallback sha256 9102249a… | **verified**: `tests/test_rizin.py` 32 passed; pecli pipeline + Rust remake 8/8 (oracle run under wine) | gate: native run of pecli.exe |
| Native ELF | ELF header | Rizin | as above | verified (sample_elf in tests) | n/a |
| .NET / Unity Mono | CLR header | ILSpy `ilspycmd` | 9.1.0.7988 on .NET 8.0.131 | **verified**: `tests/test_ilspy.py`; dotnetapp pipeline recovers C#; oracle 9/9 self-check | gate: dotnetapp.exe apphost |
| Unity IL2CPP | GameAssembly.dll + global-metadata.dat | Cpp2IL (not integrated) | – | detected only → unsupported item in plan | experimental |
| Godot 4 PCK | GDPC magic / embedded trailer | GDRE tools headless | v2.7.0 (sha256 abb4c197…) | **verified**: `tests/test_gdre.py`; godotgame recovered byte-identical; Bevy scaffold builds (~5 min) | gate: GDRE windows asset hash |
| GameMaker / Android / JVM / Unreal | data.win / apk / jar / pak | not integrated | – | detected → unsupported item | experimental |
| JS / Electron | package.json, app.asar, source maps, SW/manifest | native asar reader + bounded extraction | @electron/asar 4.3.1 (fixture build) | **verified**: `tests/test_jsweb.py`; webapp pipeline 4/4 features verified incl. offline + pixel-exact screenshots; independent harness exact hashes | gate: WebView2/browser on Windows |
| Target: Rust | – | cargo | rustc 1.97.0 | verified (pecli remake) | gate: `--target x86_64-pc-windows-msvc` build |
| Target: Rust + Bevy | – | cargo + bevy 0.18 | pinned in scaffold | builds (scaffold); gameplay parity untested | gate |
| Target: HTML/CSS/JS + PWA | – | static build + Playwright comparator | playwright 1.58.2 + Chromium 1194 | verified (webapp) | gate: Edge/WebView2 |
| Windows installer/portable | – | Tauri 2.12.1 NSIS | pinned in desktop/src-tauri/Cargo.toml | `cargo check` passes on Linux | gate: build + install + launch |
| Hermes computer use | – | hermes-agent CLI + cua-driver MCP | commit daefc2b7; cua-driver 0.21.0 | recorded-protocol tests 36 passed | gates G1–G9 (docs/HERMES.md) |
| AI providers | – | OpenAI Responses / Anthropic / Gemini / OpenRouter / local | httpx | 208 mocked protocol tests; 0 live calls | opt-in live checks with keys |
