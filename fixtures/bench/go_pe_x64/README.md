# Benchmark row `go_pe_x64`

Go windows/amd64, -ldflags '-s -w' (symbol table and DWARF stripped).

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchgo.exe` (2760192 bytes, sha256 `21f80550484b981ce47a64c751a57842938318a501bd72d8e48f0147a330dae2`) |
| Source | `fixtures/bench/go_pe_x64/src` |
| Toolchain | go version go1.27.0 windows/amd64 |
| Stripped/obfuscated how | -ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary) |
| Truth from | go tool nm -size -sort address on the unstripped build (identical .text, verified) |
| Truth size | 2810 functions (9 from our source), 47 imports, 6 notable strings |

## Recorded build

```
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o benchgo.exe .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_benchgo.exe .
```

Rebuild: `python fixtures/bench/build_bench.py --rows go_pe_x64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows go_pe_x64`.
