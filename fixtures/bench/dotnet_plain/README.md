# Benchmark row `dotnet_plain`

.NET 8 console (Release, Deterministic), names intact.

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchnet.dll` (11264 bytes, sha256 `93cb94b80069b8ef8893241c862c73b6f3f8b43e5d49ea8b88fa1e177681a9f4`) |
| Source | `fixtures/bench/dotnet_plain/src` |
| Toolchain | dotnet SDK 8.0.424 |
| Stripped/obfuscated how | no PDB (DebugType=none) |
| Truth from | ECMA-335 metadata of the unobfuscated build |
| Truth size | 12 types, 30 methods, 5 notable strings |

## Recorded build

```
dotnet build benchnet.csproj -c Release (Deterministic, Optimize, DebugType=none)
```

Rebuild: `python fixtures/bench/build_bench.py --rows dotnet_plain`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows dotnet_plain`.
