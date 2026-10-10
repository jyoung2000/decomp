# Benchmark row `rust_msvc_x64`

Rust release (opt-level 3), x86_64-pc-windows-msvc, PDB not shipped.

Ground truth for the R0 scoreboard (`scripts/benchmark.py`). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchrs.exe` (159744 bytes, sha256 `79ade84727af733b9f92a969e9a37c9aea5c01c94bb1eac645d53f12c3ec525d`) |
| Source | `fixtures/bench/rust_msvc_x64/src` |
| Toolchain | rustc 1.98.1 (48a229cea 2026-09-01); cargo 1.98.1 (797e8a9bc 2026-08-05) |
| Stripped/obfuscated how | the PDB is not shipped; MSVC-target executables carry no symbol table |
| Truth from | benchrs.pdb (MSF 7.00, read by tools/pdb_truth.py) |
| Truth size | 386 functions (9 from our source), 87 imports, 5 notable strings |

## Recorded build

```
RUSTFLAGS='--remap-path-prefix=<src>=benchrs -C link-arg=/Brepro -C link-arg=/PDBALTPATH:benchrs.pdb' cargo build --release --target x86_64-pc-windows-msvc
Cargo.toml [profile.release]: opt-level=3 debug=2 strip=none codegen-units=1 lto=false panic=unwind
```

Rebuild: `python fixtures/bench/build_bench.py --rows rust_msvc_x64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows rust_msvc_x64`.
