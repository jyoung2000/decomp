# R0 benchmark corpus (`fixtures/bench`)

Small CLI programs we wrote, built stripped/optimized/packed/renamed, each with ground truth taken from the symbol-bearing
build. `scripts/benchmark.py` runs the product's own analysis (rizin backend for native code, ILSpy backend for .NET) on
`<row>/bin/` and scores it against `<row>/truth/truth.json`; results go to `reports/benchmark.{json,md}`.

| Row | Kind | What |
|-----|------|------|
| [`c_msvc_x64_o2`](c_msvc_x64_o2/README.md) | PE x64 | C, MSVC `/O2 /MD`, PDB not shipped |
| [`c_msvc_x86_o2`](c_msvc_x86_o2/README.md) | PE x86 | same C source, 32-bit |
| [`cpp_msvc_x64_o2`](cpp_msvc_x64_o2/README.md) | PE x64 | C++17 classes, vtables, RTTI, exceptions, templates |
| [`go_pe_x64`](go_pe_x64/README.md) | PE x64 | Go, `-ldflags "-s -w"` |
| [`go_elf_x64`](go_elf_x64/README.md) | ELF x64 | same Go source, linux/amd64, stripped (the stripped-ELF row) |
| [`rust_msvc_x64`](rust_msvc_x64/README.md) | PE x64 | Rust release, x86_64-pc-windows-msvc, PDB not shipped |
| [`upx_c_msvc_x64`](upx_c_msvc_x64/README.md) | PE x64 packed | `c_msvc_x64_o2` packed with UPX 5.2.1 `--best` |
| [`dotnet_plain`](dotnet_plain/README.md) | .NET 8 | C# task scheduler, names intact |
| [`dotnet_renamed`](dotnet_renamed/README.md) | .NET 8 | same assembly, ConfuserEx-style renaming by `tools/dotnet_rename.py` (still runs) |

Rows not built on the build host are listed in `manifest.json` → `not_built` with the reason (mingw-w64, gcc ELF, Unity IL2CPP,
GameMaker, ConfuserEx itself). They appear in the scoreboard as "not run: <reason>", never as 0 or as a pass.

## Ground truth (`truth/truth.json`, schema `rebuild-studio.bench-truth/1`)
* `functions`: `name`, `aliases`, `start` (VA, hex), `size`, `module`, `own` (from our source file / namespace), unique by start.
  MSVC and Rust: procedure + public symbols of the PDB, read by `tools/pdb_truth.py` (independent of rizin; the PDB itself is not committed).
  Go: `go tool nm -size` on an unstripped twin built with identical flags; the build fails unless both `.text` sections are byte-identical.
* `code_ranges`: executable sections (precision only counts functions found inside them), `imports` (`lib`, `name`) from the
  unpacked PE's import table, `strings`: notable literals from our source (the build checks each is in the binary).
* .NET: `dotnet.types` and `dotnet.methods` from the unobfuscated assembly's metadata (via the controller's ECMA-335 reader).
* `packed` / `packer` for the UPX row (its truth is the unpacked build).

## Commands
* `python fixtures/bench/build_bench.py [--rows a,b]` rebuilds rows, re-extracts truth, rewrites `manifest.json` (needs the
  toolchains named in each `build.json`; MSVC is found with vswhere, UPX must match the pin in `docs/dependency-lock.json` → `fixture_build_tools.upx`).
* `python fixtures/bench/build_bench.py --verify` rebuilds into a temp dir and fails unless every committed binary is reproduced byte for byte
  (MSVC `/Brepro`, Go `-trimpath -buildid=`, Rust `--remap-path-prefix` + `/Brepro`, .NET `Deterministic`).
* `python scripts/benchmark.py [--rows ...] [--config cfg.json]` scores the corpus (needs rizin + rz-ghidra via `REBUILD_STUDIO_TOOLS`, ilspycmd).

Limits kept: every binary < 5 MB, corpus total < 25 MB (checked by the build and by `controller/tests/test_benchmark.py`).
