# Benchmark row `c_msvc_x86_o2`

same C source, MSVC /O2, 32-bit PE (x86), PDB not shipped.

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchc32.exe` (12800 bytes, sha256 `05f9dccd75e219c4223411b1f0edfc691f123d5694ad0705136c0f29560465be`) |
| Source | `fixtures/bench/c_msvc_x64_o2/src/benchc.c` |
| Toolchain | MSVC cl 19.29.30159 (x86), VC tools 14.29.30133, Windows SDK 10.0.19041.0 |
| Stripped/obfuscated how | the PDB is not shipped; the exe only names it (PDBALTPATH) and carries no symbol table |
| Truth from | benchc32.pdb (MSF 7.00, read by tools/pdb_truth.py) |
| Truth size | 95 functions (22 from our source), 55 imports, 10 notable strings |

## Recorded build

```
cl /nologo /O2 /MD /W3 /GS /Zi /Brepro /DNDEBUG /utf-8 /c benchc.c
link /nologo /DEBUG:FULL /PDB:benchc32.pdb /PDBALTPATH:benchc32.pdb /OPT:REF /OPT:ICF /INCREMENTAL:NO /Brepro /SUBSYSTEM:CONSOLE /OUT:benchc32.exe benchc.obj
```

Rebuild: `python fixtures/bench/build_bench.py --rows c_msvc_x86_o2`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows c_msvc_x86_o2`.
