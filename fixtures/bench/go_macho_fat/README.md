# Benchmark row `go_macho_fat`

fat/universal Mach-O (x86_64 + arm64) assembled from the two thin rows by tools/macho_fat.py (no lipo on the host); slices are byte-identical to the thin rows.

Ground truth for the scoreboard (`scripts/benchmark.py`, R7 input kinds). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchgo_universal` (5182130 bytes, sha256 `331f295abd9c1b54aac10a6b7fe9c8a87fec10b850cafa7579fae5f5906f305c`) |
| Source | `fixtures/bench/go_pe_x64/src` |
| Toolchain | go version go1.27.0 windows/amd64; fat header by fixtures/bench/tools/macho_fat.py |
| Stripped/obfuscated how | -ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary); each slice is the stripped thin build |
| Truth from | go tool nm -size -sort address on the unstripped build (identical .text, verified); per slice (slices byte-identical to the thin rows) |
| Truth size | x86_64: 2770 functions (9 from our source), slice sha256 `421b4b7446c542b46ecadc83063774b34883e43f84564dee05d3249fd988ec1c` = `go_macho_x64`; arm64: 2732 functions (9 from our source), slice sha256 `b8938d00ba5a39a74ec63eaf8ef7f61c1e1daaf30485246c870f2daffb11a7a3` = `go_macho_arm64` |

A fat/universal file needs `lipo`, which does not exist on Windows; `fixtures/bench/tools/macho_fat.py` writes the documented `fat_header`/`fat_arch` layout (big-endian, slice alignment 2^12 for x86_64 and 2^14 for arm64) and copies the two thin rows byte for byte. That writer is deliberately independent of the controller's Mach-O parser, so the product's slice extraction (`backends/members.py`) is checked against it. The scoreboard scores each slice on its own (`go_macho_fat [x86_64]`, `go_macho_fat [arm64]`); the numbers must equal the thin rows'.

## Recorded build

```
GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o benchgo .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_benchgo .
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o benchgo .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_benchgo .
python fixtures/bench/tools/macho_fat.py benchgo_universal go_macho_x64/benchgo go_macho_arm64/benchgo
```

Rebuild: `python fixtures/bench/build_bench.py --rows go_macho_fat`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows go_macho_fat`.
