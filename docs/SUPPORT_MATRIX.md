# Support matrix (updated 2026-10-06; "verified" = a fixture regression passed on the named host; Windows column = certified on Windows)

| Profile | Detection | Recovery backend | Pinned version | Linux host (this session) | Windows |
|---------|-----------|------------------|----------------|---------------------------|---------|
| Native PE x64 | pefile/LIEF (CLR header, subsystem, imports) | Rizin via rzpipe + rz-ghidra decompiler | rizin v0.9.1 (c3a90e92) source build; rz-ghidra v0.9.0 (999df7b8); static fallback sha256 9102249a… | **verified**: `tests/test_rizin.py` 32 passed; pecli pipeline + Rust remake 8/8 (oracle run under wine) | gate: native run of pecli.exe |
| Native ELF | ELF header | Rizin | as above | verified (sample_elf in tests) | n/a |
| .NET / Unity Mono | CLR header | ILSpy `ilspycmd` | 9.1.0.7988 on .NET 8.0.131 | **verified**: `tests/test_ilspy.py`; dotnetapp pipeline recovers C#; oracle 9/9 self-check | gate: dotnetapp.exe apphost |
| Unity IL2CPP | UnityPlayer.dll/GameAssembly + `*_Data/il2cpp_data/Metadata/global-metadata.dat` (magic 0xFAB11BAF) | none (detected-only; `triage` lists metadata identifier names, no code) | – | detected-only → unsupported item in plan; `tests/test_support_triage.py` (synthetic headers) | detected-only |
| Godot 4 PCK | GDPC magic / embedded trailer | GDRE tools headless | v2.7.0 (sha256 abb4c197…) | **verified**: `tests/test_gdre.py`; godotgame recovered byte-identical; Bevy scaffold builds (~5 min) | gate: GDRE windows asset hash |
| GameMaker | data.win/game.unx FORM+GEN8 | none (detected-only; `triage` gives chunk table, bytecode version, strings) | – | `tests/test_support_triage.py` (synthetic) | detected-only |
| Unreal Engine | `*.pak` footer magic 0x5A6F12E1, `.utoc`, Engine/Binaries, `++UE4/5+Release-x.y` | none (detected-only; `triage` gives pak footer + engine version) | – | `tests/test_support_triage.py` (synthetic) | detected-only |
| Mach-O (thin/fat) | MH_MAGIC(_64)/FAT_MAGIC(_64) incl. byte-swapped, fat vs Java-class disambiguation | none (detected-only; `triage` gives slices/dylibs/encryption flag) | – | `tests/test_support_triage.py` (synthetic) | detected-only |
| JVM (.jar/.class) | zip with META-INF/MANIFEST.MF or *.class; class-file magic | CFR via `java -jar` (`backends/jvm.py`) | CFR 0.152 (sha256 f686e8f3...); Temurin JRE 17.0.20.1 | **verified**: `tests/test_jvm.py`; javacli recovered by CFR recompiles with javac and replays 11/11 oracle scenarios | tests ran natively on Windows 11 |
| Android (.apk/.aab/.dex) | zip with AndroidManifest.xml + classes*.dex; BundleConfig.pb | built-in AXML/dex inspector always; jadx 1.5.6 for code (optional tool) | jadx 1.5.6 (sha256 545ea2be...) | partial: manifest/dex evidence tested always; jadx recovery tested on a minimal APK when jadx is installed | native Windows |
| JS / Electron | package.json, app.asar, source maps, SW/manifest | native asar reader + bounded extraction | @electron/asar 4.3.1 (fixture build) | **verified**: `tests/test_jsweb.py`; webapp pipeline 4/4 features verified incl. offline + pixel-exact screenshots; independent harness exact hashes | gate: WebView2/browser on Windows |
| Target: Rust | – | cargo | rustc 1.97.0 | verified (pecli remake) | gate: `--target x86_64-pc-windows-msvc` build |
| Target: Rust + Bevy | – | cargo + bevy 0.18 | pinned in scaffold | builds (scaffold); gameplay parity untested | gate |
| Target: HTML/CSS/JS + PWA | – | static build + Playwright comparator | playwright 1.58.2 + Chromium 1194 | verified (webapp) | gate: Edge/WebView2 |
| Windows installer/portable | – | Tauri 2.12.1 NSIS | pinned in desktop/src-tauri/Cargo.toml | `cargo check` passes on Linux | gate: build + install + launch |
| Hermes computer use | – | hermes-agent CLI + cua-driver MCP | commit daefc2b7; cua-driver 0.21.0 | recorded-protocol tests 36 passed | gates G1–G9 (docs/HERMES.md) |
| AI providers | – | OpenAI Responses / Anthropic / Gemini / OpenRouter / local | httpx | 208 mocked protocol tests; 0 live calls | opt-in live checks with keys |
| Desktop UI (React/TS) | – | Vite build; Playwright e2e | React 18.3.1, @playwright/test 1.56.1 | 68 vitest; 18 e2e vs mock; 4/4 real-controller spec (web fixture end to end, stale/disconnect detection) | gate W9: sizes/DPI matrix on Windows |
| Cutter plugin | – | Cutter Python plugin + stdlib client | cutter commit d7f11b22 (2.5.0 dev) | 20 client tests vs real controller; Qt dock smoke offscreen | gate: load inside Cutter on Windows |
| Windows packaging | – | PowerShell scripts + NSIS + PyInstaller | pwsh 7.5.4 parse; pyinstaller pinned in scripts/windows | parse/dry-run/actionlint only | gates W1–W20 (docs/WINDOWS_RELEASE_GATES.md) |

## Measured analysis quality (R0 benchmark)

Numbers, not adjectives: [`reports/benchmark.md`](../reports/benchmark.md) scores the no-AI analysis (rizin 0.9.1 + rz-ghidra for native code, ILSpy for .NET) against ground truth from our own sources in `fixtures/bench/` (MSVC C/C++ x64/x86, Go PE + stripped ELF, Rust, UPX-packed PE, .NET plain + renamed): function-boundary recall/precision, named-function recall, real-decompiler coverage, imports/strings recall, packed detection, per-stage time. Rows the build host could not produce (mingw, gcc ELF, Unity IL2CPP, GameMaker) are listed there as "not run". Regenerate with `python scripts/benchmark.py`.

## Input kinds: what is promised, per kind

Rebuild Studio does **not** promise full source recovery for arbitrary apps. For every input kind the detection result carries a
plain-language support statement (`backends/support.py`, field `support.statement`; installation level: `inventory.profile.support_statement`)
that is shown before any work starts: what can be recovered, what cannot, what the rebuild will be, the blocker and the next step.
Status words: **supported** = a pinned tool recovers code and a fixture regression passed on the named host; **partial** = a tool or parser
recovers part of the material; **detected-only** = identified with evidence, inventory/header facts only, no code recovery;
**unsupported** = nothing offered. Nothing recovered is claimed to behave like the original until the scenario comparator has run.

| Input kind | Detection | Recovery | Rebuild target | Verification | Status | Evidence (test names) |
|------------|-----------|----------|----------------|--------------|--------|-----------------------|
| Native PE/ELF | PE/ELF magic, pefile | Rizin + rz-ghidra pseudo-C | clean-room re-implementation in the chosen target | pecli oracle | supported | `tests/test_rizin.py`, `tests/test_fixture_oracle.py::test_pecli_oracle_rejects_wrong_remake_and_accepts_original` |
| .NET assembly | CLR header | ILSpy C# per type | re-implementation guided by recovered C# | dotnetapp oracle | supported | `tests/test_ilspy.py` |
| Unity, Mono scripting backend | `*_Data/Managed/Assembly-CSharp*.dll` (+ UnityPlayer.dll); never IL2CPP | routed to ILSpy (`unity_mono` -> `recover_managed`) | re-implementation of the scripts; no Unity project/assets regenerated | Assembly-CSharp sample; no full Unity game fixture | supported (scripts only) | `tests/test_support_triage.py::test_unity_mono_is_detected_and_routed_to_ilspy`, `tests/test_ilspy.py` |
| Unity, IL2CPP | `GameAssembly.*` + `il2cpp_data/Metadata/global-metadata.dat` sanity header | none; `triage inspect` lists identifier names and metadata version | none from code (plan marks unsupported); candidates Il2CppDumper (MIT), Cpp2IL (MIT) | synthetic headers only | detected-only | `tests/test_support_triage.py::test_unity_il2cpp_needs_metadata_and_is_detected_only`, `::test_unity_gameassembly_without_metadata_lowers_confidence`, `::test_il2cpp_metadata_identifiers_are_listed_and_capped` |
| GameMaker | `data.win`/`game.unx` FORM+GEN8, `audiogroup*.dat` | none; `triage inspect` gives chunk table, bytecode version, VM-vs-YYC hint, strings | none from code; candidate UndertaleModTool (GPL-3.0, separate process only) | synthetic FORM/GEN8 only | detected-only | `tests/test_support_triage.py::test_gamemaker_detection_chunks_and_strings`, `::test_gamemaker_without_code_chunk_is_flagged_likely_yyc_and_strings_are_capped`, `::test_gamemaker_installation_profile` |
| Unreal Engine | `.pak` footer, `.utoc` IoStore magic, Engine/Binaries, Shipping exe, Build.version / `++UE` string | none; `triage inspect` gives pak version/index/encryption flag; engine version from markers | none from code; candidates CUE4Parse (Apache-2.0), FModel (GPL-3.0) | synthetic pak footers only | detected-only | `tests/test_support_triage.py::test_unreal_pak_footer_detection`, `::test_unreal_encrypted_pak_and_iostore_and_non_pak`, `::test_unreal_installation_with_version_marker`, `::test_unreal_build_version_json_wins` |
| Mach-O (thin, fat/universal) | 4 thin magics + FAT_MAGIC(_64), fat-vs-class check, LC_ENCRYPTION flag | none; `triage inspect` gives slices, filetype, platform, dylibs, encryption | none from code (no Mach-O regression, host cannot run Mach-O) | synthetic thin/fat headers only | detected-only | `tests/test_support_triage.py::test_macho_thin_detection_with_support_statement`, `::test_macho_fat_universal_lists_every_slice`, `::test_macho_encrypted_slice_is_reported_as_blocker`, `::test_java_class_is_not_mistaken_for_fat_macho` |
| Java / JVM (.jar, .class) | zip + MANIFEST.MF/`*.class`; class magic with version | CFR 0.152 per-class `.java` + recovery report (failed methods, warnings, timeout/truncation disclosure) | recovered Java is reference code; delivered rebuild judged by scenarios (a Java target is NOT wired into `stages.py` yet) | javacli: recompiled recovered sources replay 11/11 frozen scenarios; a mutated recovery is rejected | supported | `tests/test_jvm.py::test_cfr_decompiles_javacli_with_per_class_report_and_evidence`, `::test_cfr_output_recompiles_and_replays_the_frozen_oracle`, `::test_original_jar_replays_its_own_oracle`, `::test_wrong_recovery_is_rejected_by_the_same_oracle`, `::test_probe_installed_pinned_tools_and_smoke_usable`, `tests/test_support_triage.py::test_jar_class_and_jvm_installation_detection` |
| Android (.apk / .aab / .dex) | zip with AndroidManifest.xml + classes*.dex (apk), BundleConfig.pb (aab), dex magic | always: AXML manifest + dex class list + native-lib/framework hints; with jadx: Java-like sources + decoded resources | reference sources only; the APK is not repackaged; blocker without jadx: "code recovery needs jadx" | minimal APK built with build-tools 34; the jadx test needs the optional tool | partial | `tests/test_support_triage.py::test_apk_detection_with_manifest_and_dex_evidence`, `::test_aab_and_framework_hints`, `tests/test_jvm.py::test_inspect_apk_manifest_and_dex_without_tools`, `::test_apk_recovery_without_jadx_states_the_blocker`, `::test_jadx_recovers_apk_code_and_decodes_manifest` |
| Godot 4 PCK | GDPC magic / trailer | GDRE tools | re-implementation (Bevy scaffold) | godotgame | supported | `tests/test_gdre.py` |
| Electron / web | asar, package.json, source maps | native asar reader | web/PWA re-implementation | webapp | supported | `tests/test_jsweb.py` |

Not recovered anywhere in this table: assets of Unity/Unreal/GameMaker/Android (textures, audio, scenes, Blueprints, `res/` beyond what jadx decodes),
native libraries inside APKs, obfuscated or packed code (reported, never repaired), and encrypted containers (pak with AES key, FairPlay Mach-O).
Candidate future tools are recorded in `backends/support.py::FUTURE_TOOLS` with upstream licenses (to be re-verified before adoption); none is bundled.

### Java/Android tool pins (docs/dependency-lock.json; all optional, installed side by side under the tools folder)
| Lock entry | Version | License | sha256 (artifact) | Provenance |
|------------|---------|---------|-------------------|------------|
| `temurin-jre` (tools/jre) | Eclipse Temurin 17.0.20.1+1 JRE x64 Windows zip | GPL-2.0 + Classpath Exception (separate process) | bc21a939...d352 | downloaded 2026-10-06, equals the checksum file published by Adoptium |
| `cfr` (tools/cfr/cfr-0.152.jar) | 0.152 | MIT | f686e8f3...65b2 | downloaded 2026-10-06 (no vendor checksum is published: trust-on-first-use, fixture-verified) |
| `jadx` (tools/jadx) | 1.5.6 | Apache-2.0 | 545ea2be...8974 | downloaded 2026-10-06, equals the GitHub release asset digest |

Re-run the Java evidence: `cd controller && ../.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_jvm.py tests/test_support_triage.py`
(CFR is downloaded to the temp dir on first run unless `REBUILD_TEST_NO_DOWNLOAD=1`; jadx only with `REBUILD_TEST_DOWNLOAD_JADX=1` or a pinned install).
