# Benchmark scoreboard (R0)

Generated 2026-10-10T15:57:51Z by `scripts/benchmark.py` with config **default** (analysis `aaa`, timeout 300 s, decompile budget 200 own functions).

Native engine: rizin 0.9.1 (commit c3a90e9226d9), rz-ghidra plugin: loaded. 
.NET engine: ilspycmd 9.1.0.7988 (C:\Users\jalon\AppData\Local\RebuildStudio\tools\ilspycmd\ilspycmd.dll).

Ground truth: our own sources built with symbols (PDB / Go symbol table / unobfuscated metadata); see `fixtures/bench/README.md`. Function boundaries are matched by exact start address. Precision counts only functions found inside executable sections. Decompiler coverage = own source functions with real rz-ghidra output / all own source functions in the truth.

## Native rows

| Row | Truth fns | Found | Boundary recall | Boundary precision | Own-fn recall | Named recall | Real-decompiler coverage (own) | Decompile failures | Imports recall | Strings recall | Packed detected | Analysis s | Decompile s | Total s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c_msvc_x64_o2 | 95 | 78 | 82.1% | 100.0% | 86.4% | 13.7% | 86.4% (19/22) | 3 | 100.0% | 100.0% | - | 2.5 | 0.5 | 4.0 |
| c_msvc_x86_o2 | 95 | 86 | 90.5% | 100.0% | 90.9% | 12.6% | 90.9% (20/22) | 2 | 100.0% | 100.0% | - | 2.5 | 0.3 | 3.0 |
| cpp_msvc_x64_o2 | 184 | 117 | 63.6% | 100.0% | 39.5% | 9.2% | 39.5% (15/38) | 23 | 96.9% | 100.0% | - | 2.7 | 1.4 | 4.5 |
| go_elf_x64 | 2715 | 2719 | 99.2% | 99.1% | 100.0% | 93.7% | 100.0% (9/9) | 0 | n/a | 100.0% | - | 40.4 | 1.5 | 44.6 |
| go_pe_x64 | 2810 | 2815 | 99.2% | 99.0% | 100.0% | 94.5% | 100.0% (9/9) | 0 | 100.0% | 100.0% | - | 30.4 | 0.8 | 34.8 |
| rust_msvc_x64 | 386 | 287 | 65.0% | 87.5% | 77.8% | 3.1% | 77.8% (7/9) | 2 | 100.0% | 100.0% | - | 6.3 | 1.3 | 8.3 |
| upx_c_msvc_x64 | 95 | 3 | 0.0% | n/a | 0.0% | 0.0% | 0.0% (0/22) | 22 | 15.8% | 0.0% | missed (no packer check) | 4.9 | 0.0 | 5.3 |
| pecli (partial) | n/a | 107 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 4.9 | - | 5.5 |

## .NET rows

| Row | Truth types | Type recall | Truth methods | Method recall | Types decompiled | Decompile failures | Strings recall | Decompile s |
|---|---|---|---|---|---|---|---|---|
| dotnet_plain | 9 | 100.0% | 10 | 100.0% | 100.0% (8/8) | 0 | 100.0% | 2.6 |
| dotnet_renamed | 9 | 11.1% | 10 | 20.0% | 100.0% (8/8) | 0 | 100.0% | 2.1 |
| dotnetapp (partial) | 4 | 100.0% | 7 | 100.0% | 100.0% (4/4) | 0 | n/a | 1.5 |

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

* `Packed detected`: the pipeline has no packer/entropy check yet, so a packed row is reported as missed until R1 adds one.
* Named recall counts a function as named only when rizin's name at the true start equals a truth name (modulo prefixes, case, punctuation); auto names (`fcn.*`, `entry0`) never count.
* Failures per row are listed in `reports/benchmark.json` (`decompile.failures`, `imports.missing_sample`, `strings.missing`).
* Reproduce: `python fixtures/bench/build_bench.py --verify` (corpus), then `python scripts/benchmark.py` (needs rizin; REBUILD_STUDIO_TOOLS).
