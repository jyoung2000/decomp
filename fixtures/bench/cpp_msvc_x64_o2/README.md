# Benchmark row `cpp_msvc_x64_o2`

C++17 classes, vtables, RTTI, exceptions, templates; MSVC /O2 /EHsc /GR, x64.

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchcpp.exe` (29696 bytes, sha256 `7141fcfdfdb7a7c989792cdb1e8f5b991c0b42d22d6f3346d4a50e9fa5cb4c95`) |
| Source | `fixtures/bench/cpp_msvc_x64_o2/src/benchcpp.cpp` |
| Toolchain | MSVC cl 19.29.30159 (x64), VC tools 14.29.30133, Windows SDK 10.0.19041.0 |
| Stripped/obfuscated how | the PDB is not shipped; the exe only names it (PDBALTPATH) and carries no symbol table |
| Truth from | benchcpp.pdb (MSF 7.00, read by tools/pdb_truth.py) |
| Truth size | 184 functions (38 from our source), 65 imports, 8 notable strings |

## Recorded build

```
cl /nologo /O2 /MD /W3 /GS /Zi /Brepro /DNDEBUG /utf-8 /EHsc /GR /std:c++17 /c benchcpp.cpp
link /nologo /DEBUG:FULL /PDB:benchcpp.pdb /PDBALTPATH:benchcpp.pdb /OPT:REF /OPT:ICF /INCREMENTAL:NO /Brepro /SUBSYSTEM:CONSOLE /OUT:benchcpp.exe benchcpp.obj
```

Rebuild: `python fixtures/bench/build_bench.py --rows cpp_msvc_x64_o2`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows cpp_msvc_x64_o2`.
