# Benchmark row `dotnet_renamed`

dotnet_plain with ConfuserEx-style identifier renaming (fixtures/bench/tools/dotnet_rename.py).

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchnet.dll` (11264 bytes, sha256 `748477c87f1cba369179a9a3322289646fead16f3dc2eea67b799074aea17b0e`) |
| Source | `fixtures/bench/dotnet_plain/src` |
| Toolchain | dotnet SDK 8.0.424 |
| Stripped/obfuscated how | identifiers renamed in the #Strings heap (36 renamed, 31 kept) |
| Truth from | ECMA-335 metadata of the unobfuscated build |
| Truth size | 12 types, 30 methods, 5 notable strings |
| Renaming | 36 identifiers renamed, 31 kept (reasons in truth.json `rename_report`) |

## Recorded build

```
dotnet build benchnet.csproj -c Release (Deterministic, Optimize, DebugType=none)
python fixtures/bench/tools/dotnet_rename.py benchnet.dll <out>/benchnet.dll
```

Rebuild: `python fixtures/bench/build_bench.py --rows dotnet_renamed`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows dotnet_renamed`.
