# Benchmark scoreboard (R0 corpus)

Generated 2026-10-10T17:08:41Z by `scripts/benchmark.py` with config **default + optional Ghidra** (analysis `aaa` + passes `sigpacks,pdata,relocptrs,thunks`, timeout 300 s, decompile budget 200 own functions, packer check on).

Native engine: rizin 0.9.1 (commit c3a90e9226d9), rz-ghidra plugin: loaded. 
.NET engine: ilspycmd 9.1.0.7988 (C:\Users\jalon\AppData\Local\RebuildStudio\tools\ilspycmd\ilspycmd.dll).

Ground truth: our own sources built with symbols (PDB / Go symbol table / unobfuscated metadata); see `fixtures/bench/README.md`. Function boundaries are matched by exact start address. Precision counts only functions found inside executable sections. Decompiler coverage = own source functions with real rz-ghidra output / all own source functions in the truth.

## Native rows

| Row | Truth fns | Found | Boundary recall | Boundary precision | Own-fn recall | Named recall | Name precision | Real-decompiler coverage (own) | Decompile failures | Imports recall | Strings recall | Packed detected | Analysis s | Decompile s | Total s | Native-language rebuild |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c_msvc_x64_o2 | 95 | 87 | 91.6% | 100.0% | 86.4% | 47.4% | 93.8% | 86.4% (19/22) | 3 | 100.0% | 100.0% | - | 1.3 | 0.3 | 19.6 | n/a (no original-language source; Rust port) |
| c_msvc_x86_o2 | 95 | 91 | 95.8% | 100.0% | 95.5% | 53.7% | 98.1% | 95.5% (21/22) | 1 | 100.0% | 100.0% | - | 1.3 | 0.2 | 16.6 | n/a (no original-language source; Rust port) |
| cpp_msvc_x64_o2 | 184 | 155 | 84.2% | 100.0% | 79.0% | 32.1% | 72.0% | 79.0% (30/38) | 8 | 96.9% | 100.0% | - | 1.4 | 0.4 | 23.8 | n/a (no original-language source; Rust port) |
| go_apk_arm64 [lib/arm64-v8a/libbenchgo.so] | 2666 | 2723 | 99.3% | 97.2% | 100.0% | 91.8% | 95.7% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 13.8 | 0.2 | 17.0 | n/a (no original-language source; Rust port) |
| go_elf_x64 | 2715 | 2719 | 99.2% | 99.1% | 100.0% | 93.7% | 95.0% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 11.7 | 0.6 | 16.3 | n/a (no original-language source; Rust port) |
| go_macho_arm64 | 2732 | 2880 | 99.2% | 95.5% | 100.0% | 91.5% | 93.6% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 12.0 | 0.2 | 13.2 | n/a (no original-language source; Rust port) |
| go_macho_fat [x86_64] | 2770 | 2850 | 99.2% | 97.1% | 100.0% | 91.7% | 93.0% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 6.4 | 0.3 | 7.5 | n/a (no original-language source; Rust port) |
| go_macho_fat [arm64] | 2732 | 2880 | 99.2% | 95.5% | 100.0% | 91.5% | 93.6% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 11.9 | 0.2 | 13.2 | n/a (no original-language source; Rust port) |
| go_macho_x64 | 2770 | 2850 | 99.2% | 97.1% | 100.0% | 91.7% | 93.0% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 6.4 | 0.3 | 7.4 | n/a (no original-language source; Rust port) |
| go_pe_x64 | 2810 | 2822 | 99.4% | 99.0% | 100.0% | 94.5% | 95.9% | 100.0% (9/9) | 0 | 100.0% | 100.0% | - | 15.8 | 0.4 | 17.9 | n/a (no original-language source; Rust port) |
| rust_msvc_x64 | 386 | 363 | 84.5% | 89.8% | 100.0% | 25.1% | 57.1% | 100.0% (9/9) | 0 | 100.0% | 100.0% | - | 2.1 | 0.4 | 37.3 | n/a (no original-language source; Rust port) |
| upx_c_msvc_x64 | 95 | 87 | 91.6% | 100.0% | 86.4% | 47.4% | 93.8% | 86.4% (19/22) | 3 | 100.0% | 100.0% | yes (UPX), unpacked with upx 5.2.1 | 1.4 | 0.3 | 24.7 | n/a (no original-language source; Rust port) |
| pecli (partial) | n/a | 133 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 1.4 | - | 1.6 | n/a (no original-language source; Rust port) |

## Ghidra headless (optional second decompiler)

Whole-program `analyzeHeadless` run per row (auto-analysis, then every function decompiled, largest first). Boundary recall/precision use Ghidra's own function list.

| Row | Ghidra | Functions | Boundary recall | Boundary precision | Real-decompiler coverage (own) | Decompile failures | Seconds |
|---|---|---|---|---|---|---|---|
| c_msvc_x64_o2 | 12.1.4 | 64 | 67.4% | 100.0% | 95.5% (21/22) | 0 | 17.6 |
| c_msvc_x86_o2 | 12.1.4 | 60 | 63.2% | 100.0% | 95.5% (21/22) | 0 | 14.9 |
| cpp_msvc_x64_o2 | 12.1.4 | 120 | 65.2% | 100.0% | 81.6% (31/38) | 0 | 21.8 |
| rust_msvc_x64 | 12.1.4 | 449 | 76.9% | 66.1% | 100.0% (9/9) | 0 | 34.1 |
| upx_c_msvc_x64 | 12.1.4 | 64 | 67.4% | 100.0% | 95.5% (21/22) | 0 | 18.2 |

## .NET rows

| Row | Truth types | Type recall | Truth methods | Method recall | Types decompiled | Decompile failures | Strings recall | Decompile s | Native-language rebuild |
|---|---|---|---|---|---|---|---|---|---|
| dotnet_plain | 9 | 100.0% | 10 | 100.0% | 100.0% (8/8) | 0 | 100.0% | 0.5 | C#: builds, no scenario oracle, 2 fixes, no AI |
| dotnet_renamed | 9 | 11.1% | 10 | 20.0% | 100.0% (8/8) | 0 | 100.0% | 0.5 | C#: does not build (2 fixes) |
| dotnetapp (partial) | 4 | 100.0% | 7 | 100.0% | 100.0% (4/4) | 0 | n/a | 0.4 | C#: builds, 9/9 scenarios, 4 fixes, no AI |

## Native-language rebuild (R4)

The product pipeline with AI off: the recovered C# (ILSpy) / Java (CFR) is the candidate, built with the private .NET SDK / JDK in the sandbox after deterministic fixes, then run against the fixture's frozen scenario oracle where there is one. Only the verifier's verdict counts. Measured 2026-10-10T18:28:28Z.

| Row | Target | Builds | Scenarios matched | Deterministic fixes | AI | Toolchain | Seconds |
|---|---|---|---|---|---|---|---|
| dotnet_plain | csharp | yes | no oracle | 2 | none | .NET SDK 8.0.425 (private SDK) | 4.3 |
| dotnet_renamed | csharp | no | no oracle | 2 | none |  | 3.2 |
| dotnetapp | csharp | yes | 9/9 | 4 | none | .NET SDK 8.0.425 (private SDK) | 4.8 |
| javacli | java | yes | 11/11 | 1 | none | javac (private JDK 21) | 5.0 |

Deterministic fixes applied:

* dotnet_plain: benchnet.csproj: target framework netcoreapp8.0 -> net8.0 (ILSpy writes the pre-.NET 5 moniker)
* dotnet_plain: Properties/AssemblyInfo.cs: removed compiler-reserved attribute(s) RefSafetyRules (ILSpy prints them; csc refuses them)
* dotnet_renamed: benchnet.csproj: target framework netcoreapp8.0 -> net8.0 (ILSpy writes the pre-.NET 5 moniker)
* dotnet_renamed: Properties/AssemblyInfo.cs: removed compiler-reserved attribute(s) RefSafetyRules (ILSpy prints them; csc refuses them)
* dotnetapp: dotnetapp.csproj: target framework netcoreapp8.0 -> net8.0 (ILSpy writes the pre-.NET 5 moniker)
* dotnetapp: dotnetapp.csproj: InvariantGlobalization=true, PredefinedCulturesOnly=true from the original's runtimeconfig.json
* dotnetapp: Properties/AssemblyInfo.cs: removed compiler-reserved attribute(s) RefSafetyRules (ILSpy prints them; csc refuses them)
* dotnetapp: NotesApp/Menus.cs: restored 3 'case null:' label(s) ILSpy dropped from string switches (the IL tests null and the constant with the same branch target)
* javacli: rebuild-java.json: main class dev.rebuild.ledger.Main, javac --release 17 (class files version 61), jar javacli.jar

## Rows not run or not scored

| Row | Status |
|---|---|
| c_mingw_x64_o2s | not run: mingw-w64 (x86_64-w64-mingw32-gcc) is not installed on this host |
| c_mingw_x86_o2s | not run: mingw-w64 (i686-w64-mingw32-gcc) is not installed on this host |
| c_gcc_elf_stripped | not run: no gcc/clang/zig for an ELF C build on this host (the stripped-ELF row is go_elf_x64) |
| unity_il2cpp | not run: needs the Unity editor with the IL2CPP module to build a project we own; not installed |
| gamemaker | not run: no free GameMaker runner/CLI build is available on this host |
| dotnet_confuserex | not run as ConfuserEx itself (.NET Framework GUI/CLI, unmaintained); renaming is reproduced by dotnet_rename.py (row dotnet_renamed) |
| javacli | not scored by R0 metrics: JVM jar (CFR path); parity is measured by its scenario oracle; native-language rebuild: see that section |
| godotgame | not scored by R0 metrics: Godot PCK (GDRE path); recovery is checked by expected/resources.json |
| webapp | not scored by R0 metrics: web/asar (no native code); parity is measured by its Playwright oracle |

## Partial rows

* **pecli**: no symbol map: shipped stripped (mingw -O1 -s) and mingw is not on this host to rebuild an unstripped twin; only functions-found / imports / strings found are reported
* **dotnetapp**: names are not obfuscated, so the assembly's own metadata is the type/method truth

## Model scoreboard (R5)

Measured 2026-10-11T04:23:22Z with the product's own AI paths against http://127.0.0.1:11434/v1 (RTX 4070 12 GB); local models, $0. Naming row `c_msvc_x64_o2`: the same 4 functions for every model (the largest own functions rizin leaves with an automatic name); `match` = shares a meaningful word with the real name, `exact` = the real name. Repair: one attempt on the recovered C# of dotnetapp with the ILSpy null-case rule switched off (8/9 before the repair); only the verifier's verdict counts.

| Model | Task | Runs | Usable | Matches real name | Exact | Compiled | Verified | Median latency |
|---|---|---|---|---|---|---|---|---|
| qwen2.5:14b | naming | 4 | 4 | 3 | 0 | - | - | 2.3 s |
| deepseek-coder-v2:16b | naming | 4 | 3 | 2 | 0 | - | - | 1.6 s |
| qwen2.5-coder:14b | naming | 4 | 4 | 2 | 0 | - | - | 2.0 s |
| qwen2.5-coder:14b | repair | 1 | 1 | - | - | 1 | 0 | 290.1 s |

Naming proposals (function: real name -> proposed, latency):

* qwen2.5-coder:14b 0x140001010: `bench_base64` -> `encode_base64` (match), 10.0 s
* qwen2.5-coder:14b 0x140001350: `cmd_hist` -> `analyze_text_frequency`, 1.9 s
* qwen2.5-coder:14b 0x1400017e0: `crc_init` -> `initialize_crc_table` (match), 2.2 s
* qwen2.5-coder:14b 0x140001be0: `read_file` -> `initialize_resource`, 1.8 s
* qwen2.5:14b 0x140001010: `bench_base64` -> `base64_encode` (match), 22.8 s
* qwen2.5:14b 0x140001350: `cmd_hist` -> `process_histogram_data` (match), 2.4 s
* qwen2.5:14b 0x1400017e0: `crc_init` -> `calculate_crc_table` (match), 2.2 s
* qwen2.5:14b 0x140001be0: `read_file` -> `initialize_and_check`, 1.9 s
* deepseek-coder-v2:16b 0x140001010: `bench_base64` -> `encode_base64` (match), 22.8 s
* deepseek-coder-v2:16b 0x140001350: `cmd_hist` -> `process_data`, 1.5 s
* deepseek-coder-v2:16b 0x1400017e0: `crc_init` -> `crc32_update` (match), 1.3 s
* deepseek-coder-v2:16b 0x140001be0: `read_file` -> `process_data`, 1.7 s
* repair qwen2.5-coder:14b on dotnetapp (IL null-case rule off): answer usable, build built, verifier 8/9 (was 8/9), not verified; prompt full 23150 chars, 8179 tokens in / 1528 out, 290 s

Import into the app's scoreboard: `POST /ai/scoreboard/import` with `{"rows": <model_scoreboard.rows of benchmark.json>}`; the ladder presets then propose the measured-best local model per task.

## Notes

* `Packed detected`: the product's packer check (`backends/packer.py`: section names, UPX magic, entropy, W+X/virtual-only code sections, entry-point and import anomalies). A UPX row is then unpacked with the pinned `upx -d` into a temp work folder (consent = benchmark config `packer.unpack`) and the unpacked copy is what gets scored; `false positive` marks an unpacked row reported as packed.
* Named recall counts a function as named only when rizin's name at the true start equals a truth name (modulo prefixes, case, punctuation); auto names (`fcn.*`, `entry0`) never count. Name precision = right names / matched functions that carry any non-auto name (a wrong name misleads more than `fcn.*`; rizin's RTTI names such as `method.Foo.virtual_0` count as wrong).
* Analysis passes (R1, `backends/rizin_passes.py`): `sigpacks` = FLIRT packs built from the MSVC 14.29 runtime libraries and the Rust 1.98.1 std rlibs (`scripts/build_sigpacks.py`, pinned in `rebuild_controller/data/sigpacks/manifest.json`; never built from this corpus); `pdata` = x64 exception-directory function starts (chained entries and EH funclets skipped); `relocptrs` = functions at relocated code pointers no analysed function covers; `thunks` = `jmp [IAT]` thunks named after their import.
* Failures per row are listed in `reports/benchmark.json` (`decompile.failures`, `imports.missing_sample`, `strings.missing`).
* Reproduce: `python fixtures/bench/build_bench.py --verify` (corpus), then `python scripts/benchmark.py` (needs rizin; REBUILD_STUDIO_TOOLS).
* Native-language rebuild (R4): `python scripts/benchmark.py --only-native-rebuild` re-measures only that column (needs ILSpy + .NET SDK, CFR + JDK) and keeps the other measurements.
* Container rows (R7): `go_macho_fat [arch]` is one slice of the universal file and `go_apk_arm64 [lib/<abi>/...]` the native library inside the APK, each taken out by the product's own member extraction (`backends/members.py`, sha256-checked) and analysed like a plain file. The Mach-O and APK rows carry no import list in their truth (build_bench.py does not extract Mach-O bind info or ELF dynamic symbols), so Imports recall is n/a.
* `--merge` re-measures only the selected rows and keeps the rest of the report as it was (see the measured-at list below).

Rows measured after the main run (`--merge`): go_apk_arm64 (2026-10-11T00:05:16Z), go_macho_arm64 (2026-10-11T00:05:16Z), go_macho_fat (2026-10-11T00:05:16Z), go_macho_x64 (2026-10-11T00:05:16Z).
