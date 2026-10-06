# Support matrix (updated as tests run; "verified" means a fixture regression passed on the named host)

| Profile | Detection | Recovery backend | Pinned version | Host tested | State |
|---------|-----------|------------------|----------------|-------------|-------|
| Native PE (x86/x64) | LIEF/pefile | Rizin (rzpipe) + rz-ghidra/pdc | rizin v0.9.1 (c3a90e92), rz-ghidra v0.9.0 | Linux (static build) | pending |
| Native ELF | LIEF | Rizin | v0.9.1 | Linux | pending |
| .NET / Unity Mono | LIEF + CLI header | ILSpy ilspycmd | 9.1.0.7988 | Linux (.NET 8.0.131) | pending |
| Unity IL2CPP | metadata file | Cpp2IL | – | – | experimental/unverified |
| Godot PCK | GDPC magic | GDRE tools | v2.7.0 (sha256 abb4c197…) | Linux | pending |
| GameMaker | data.win | UndertaleModTool | – | – | experimental/unverified |
| Android/JVM | apk/jar | jadx/Apktool | – | – | experimental/unverified |
| Unreal | .pak/.utoc | CUE4Parse/FModel | – | – | experimental/unverified |
| JS / Electron | app.asar, package.json, source maps | bounded extraction (@electron/asar) | – | Linux | pending |
| Target: Rust | – | cargo | rustc 1.97.0 | Linux | pending |
| Target: Rust + Bevy | – | cargo | bevy pin TBD | Linux | pending |
| Target: HTML/CSS/JS + PWA | – | node 22 + Playwright/Chromium | – | Linux | pending |
| Windows installer/portable | – | Tauri 2 NSIS | TBD | needs Windows runner | handoff |
| Hermes computer use | – | hermes-agent (pinned commit daefc2b7) | – | needs Windows desktop | handoff |
