# Benchmark row `upx_c_msvc_x64`

c_msvc_x64_o2 packed with UPX 5.2.1 --best (truth = the unpacked build).

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchc_upx.exe` (10240 bytes, sha256 `ce95f751f9e3c96e2bd9a7ce70e340b888a975b432e5716e8e4af6653ec41af0`) |
| Source | `fixtures/bench/c_msvc_x64_o2/src/benchc.c` |
| Toolchain | MSVC cl 19.29.30159 (x64), VC tools 14.29.30133, Windows SDK 10.0.19041.0; upx 5.2.1 (sha256 d20ebe0b7b22b6be...) |
| Stripped/obfuscated how | packed: original sections compressed into UPX0/UPX1; truth is the unpacked build's PDB |
| Truth from | benchc.pdb (MSF 7.00, read by tools/pdb_truth.py) |
| Truth size | 95 functions (22 from our source), 57 imports, 10 notable strings |
| Packed | yes (UPX 5.2.1) |

## Recorded build

```
cl /nologo /O2 /MD /W3 /GS /Zi /Brepro /DNDEBUG /utf-8 /c benchc.c
link /nologo /DEBUG:FULL /PDB:benchc.pdb /PDBALTPATH:benchc.pdb /OPT:REF /OPT:ICF /INCREMENTAL:NO /Brepro /SUBSYSTEM:CONSOLE /OUT:benchc.exe benchc.obj
upx --best --no-color -q -o benchc_upx.exe benchc.exe
```

Rebuild: `python fixtures/bench/build_bench.py --rows upx_c_msvc_x64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows upx_c_msvc_x64`.
