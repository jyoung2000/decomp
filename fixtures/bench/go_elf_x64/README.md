# Benchmark row `go_elf_x64`

same Go source, linux/amd64 static ELF, stripped (-s -w); the stripped-ELF row.

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchgo` (2596988 bytes, sha256 `eb6d3c611e39a8531108ecdc3907461d394a16e84ee367e3e392907e2c16a5af`) |
| Source | `fixtures/bench/go_pe_x64/src` |
| Toolchain | go version go1.27.0 windows/amd64 |
| Stripped/obfuscated how | -ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary) |
| Truth from | go tool nm -size -sort address on the unstripped build (identical .text, verified) |
| Truth size | 2715 functions (9 from our source), 0 imports, 6 notable strings |

## Recorded build

```
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o benchgo .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_benchgo .
```

Rebuild: `python fixtures/bench/build_bench.py --rows go_elf_x64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows go_elf_x64`.
