# Benchmark row `go_macho_arm64`

same Go source, darwin/arm64 thin Mach-O (Apple silicon), stripped (-s -w); cross-compiled, never executed here.

Ground truth for the scoreboard (`scripts/benchmark.py`, R7 input kinds). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchgo` (2511538 bytes, sha256 `b8938d00ba5a39a74ec63eaf8ef7f61c1e1daaf30485246c870f2daffb11a7a3`) |
| Source | `fixtures/bench/go_pe_x64/src` |
| Toolchain | go version go1.27.0 windows/amd64 |
| Stripped/obfuscated how | -ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary) |
| Truth from | go tool nm -size -sort address on the unstripped build (identical .text, verified) |
| Truth size | 2732 functions (9 from our source), 0 imports, 6 notable strings |

Cross-compiled on Windows (`GOOS=darwin GOARCH=arm64`, Apple silicon); never executed on the build host. The Go linker ad-hoc signs darwin/arm64 binaries (LC_CODE_SIGNATURE); the signature is part of the committed bytes.

## Recorded build

```
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o benchgo .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_benchgo .
```

Rebuild: `python fixtures/bench/build_bench.py --rows go_macho_arm64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows go_macho_arm64`.
