# Benchmark scoreboard (R0 corpus)

Generated 2026-10-10T17:08:41Z by `scripts/benchmark.py` with config **default + optional Ghidra** (analysis `aaa` + passes `sigpacks,pdata,relocptrs,thunks`, timeout 300 s, decompile budget 200 own functions, packer check on).

Native engine: rizin 0.9.1 (commit c3a90e9226d9), rz-ghidra plugin: loaded. 
.NET engine: ilspycmd 9.1.0.7988 (C:\Users\jalon\AppData\Local\RebuildStudio\tools\ilspycmd\ilspycmd.dll).

Ground truth: our own sources built with symbols (PDB / Go symbol table / unobfuscated metadata); see `fixtures/bench/README.md`. Function boundaries are matched by exact start address. Precision counts only functions found inside executable sections. Decompiler coverage = own source functions with real rz-ghidra output / all own source functions in the truth.

## Native rows

| Row | Truth fns | Found | Boundary recall | Boundary precision | Own-fn recall | Named recall | Name precision | Real-decompiler coverage (own) | Decompile failures | Imports recall | Strings recall | Packed detected | Analysis s | Decompile s | Total s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c_msvc_x64_o2 | 95 | 87 | 91.6% | 100.0% | 86.4% | 47.4% | 93.8% | 86.4% (19/22) | 3 | 100.0% | 100.0% | - | 1.3 | 0.3 | 19.6 |
| c_msvc_x86_o2 | 95 | 91 | 95.8% | 100.0% | 95.5% | 53.7% | 98.1% | 95.5% (21/22) | 1 | 100.0% | 100.0% | - | 1.3 | 0.2 | 16.6 |
| cpp_msvc_x64_o2 | 184 | 155 | 84.2% | 100.0% | 79.0% | 32.1% | 72.0% | 79.0% (30/38) | 8 | 96.9% | 100.0% | - | 1.4 | 0.4 | 23.8 |
| go_elf_x64 | 2715 | 2719 | 99.2% | 99.1% | 100.0% | 93.7% | 95.0% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 11.7 | 0.6 | 16.3 |
| go_pe_x64 | 2810 | 2822 | 99.4% | 99.0% | 100.0% | 94.5% | 95.9% | 100.0% (9/9) | 0 | 100.0% | 100.0% | - | 15.8 | 0.4 | 17.9 |
| rust_msvc_x64 | 386 | 363 | 84.5% | 89.8% | 100.0% | 25.1% | 57.1% | 100.0% (9/9) | 0 | 100.0% | 100.0% | - | 2.1 | 0.4 | 37.3 |
| upx_c_msvc_x64 | 95 | 87 | 91.6% | 100.0% | 86.4% | 47.4% | 93.8% | 86.4% (19/22) | 3 | 100.0% | 100.0% | yes (UPX), unpacked with upx 5.2.1 | 1.4 | 0.3 | 24.7 |
| pecli (partial) | n/a | 133 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 1.4 | - | 1.6 |

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

| Row | Truth types | Type recall | Truth methods | Method recall | Types decompiled | Decompile failures | Strings recall | Decompile s |
|---|---|---|---|---|---|---|---|---|
| dotnet_plain | 9 | 100.0% | 10 | 100.0% | 100.0% (8/8) | 0 | 100.0% | 0.5 |
| dotnet_renamed | 9 | 11.1% | 10 | 20.0% | 100.0% (8/8) | 0 | 100.0% | 0.5 |
| dotnetapp (partial) | 4 | 100.0% | 7 | 100.0% | 100.0% (4/4) | 0 | n/a | 0.4 |

## Rows not run or not scored

| Row | Status |
|---|---|
| c_mingw_x64_o2s | not run: mingw-w64 (x86_64-w64-mingw32-gcc) is not installed on this host |
| c_mingw_x86_o2s | not run: mingw-w64 (i686-w64-mingw32-gcc) is not installed on this host |
| c_gcc_elf_stripped | not run: no gcc/clang/zig for an ELF C build on this host (the stripped-ELF row is go_elf_x64) |
| unity_il2cpp | not run: needs the Unity editor with the IL2CPP module to build a project we own; not installed |
| gamemaker | not run: no free GameMaker runner/CLI build is available on this host |
| dotnet_confuserex | not run as ConfuserEx itself (.NET Framework GUI/CLI, unmaintained); renaming is reproduced by dotnet_rename.py (row dotnet_renamed) |
| javacli | not scored by R0 metrics: JVM jar (CFR path); parity is measured by its scenario oracle |
| godotgame | not scored by R0 metrics: Godot PCK (GDRE path); recovery is checked by expected/resources.json |
| webapp | not scored by R0 metrics: web/asar (no native code); parity is measured by its Playwright oracle |

## Partial rows

* **pecli**: no symbol map: shipped stripped (mingw -O1 -s) and mingw is not on this host to rebuild an unstripped twin; only functions-found / imports / strings found are reported
* **dotnetapp**: names are not obfuscated, so the assembly's own metadata is the type/method truth

## Notes

* `Packed detected`: the product's packer check (`backends/packer.py`: section names, UPX magic, entropy, W+X/virtual-only code sections, entry-point and import anomalies). A UPX row is then unpacked with the pinned `upx -d` into a temp work folder (consent = benchmark config `packer.unpack`) and the unpacked copy is what gets scored; `false positive` marks an unpacked row reported as packed.
* Named recall counts a function as named only when rizin's name at the true start equals a truth name (modulo prefixes, case, punctuation); auto names (`fcn.*`, `entry0`) never count. Name precision = right names / matched functions that carry any non-auto name (a wrong name misleads more than `fcn.*`; rizin's RTTI names such as `method.Foo.virtual_0` count as wrong).
* Analysis passes (R1, `backends/rizin_passes.py`): `sigpacks` = FLIRT packs built from the MSVC 14.29 runtime libraries and the Rust 1.98.1 std rlibs (`scripts/build_sigpacks.py`, pinned in `rebuild_controller/data/sigpacks/manifest.json`; never built from this corpus); `pdata` = x64 exception-directory function starts (chained entries and EH funclets skipped); `relocptrs` = functions at relocated code pointers no analysed function covers; `thunks` = `jmp [IAT]` thunks named after their import.
* Failures per row are listed in `reports/benchmark.json` (`decompile.failures`, `imports.missing_sample`, `strings.missing`).
* Reproduce: `python fixtures/bench/build_bench.py --verify` (corpus), then `python scripts/benchmark.py` (needs rizin; REBUILD_STUDIO_TOOLS).
