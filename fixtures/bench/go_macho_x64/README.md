# Benchmark row `go_macho_x64`

same Go source, darwin/amd64 thin Mach-O, stripped (-s -w); built by cross-compiling on Windows (never executed here).

Ground truth for the scoreboard (`scripts/benchmark.py`, R7 input kinds). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchgo` (2651664 bytes, sha256 `421b4b7446c542b46ecadc83063774b34883e43f84564dee05d3249fd988ec1c`) |
| Source | `fixtures/bench/go_pe_x64/src` |
| Toolchain | go version go1.27.0 windows/amd64 |
| Stripped/obfuscated how | -ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary) |
| Truth from | go tool nm -size -sort address on the unstripped build (identical .text, verified) |
| Truth size | 2770 functions (9 from our source), 0 imports, 6 notable strings |

Cross-compiled on Windows (`GOOS=darwin`); never executed on the build host. rizin reads it statically.

## Recorded build

```
GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o benchgo .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_benchgo .
```

Rebuild: `python fixtures/bench/build_bench.py --rows go_macho_x64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows go_macho_x64`.
