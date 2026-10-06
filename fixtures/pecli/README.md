# pecli fixture (native Windows PE x64 CLI)

Tiny key/value store written in C (`src/pecli.c`, 328 lines) with its own binary save format. Source is ground truth
and must never be read by adapters (DR-8); adapters see only `original/`.

## Layout
| Path | Purpose |
|------|---------|
| `src/pecli.c`, `src/README.txt`, `src/make_sample.py` | ground truth source + shipped docs + independent generator of `sample.dat` |
| `build.sh` | deterministic build -> `original/` + `original.manifest.json` |
| `original/` | `pecli.exe` (PE32+ x64, 44032 bytes), `README.txt`, `sample.dat` (63 bytes) |
| `harness/` | `config.json` (wine launcher), `scenarios.json` (inputs), `selfcheck.sh` |
| `expected/scenarios.json` | frozen oracle: per step exit code, stdout, stderr, per scenario final files (size, sha256, text/hex) |
| `wrong_remake/` | cargo project (bin `pecli`) that builds but is deliberately wrong (see below) |
| `features.json` | stable feature ids |

## Save format (little endian)
`"PCLI"` magic, `u32` version (1), `u32` record count, `u32` CRC32 (IEEE, reflected, 0xEDB88320) over the record area,
then records `u16 key_len, u16 value_len, key, value`. Limits: key <= 64, value <= 1024, records <= 256.

## Commands and exit codes
`init <file>`, `add <file> <key> <value>`, `get <file> <key>`, `list <file>`, `remove <file> <key>`, `checksum <file>`.
Exit 0 ok; 1 usage / key not found; 2 bad magic or version; 3 file missing; 4 corrupt (CRC mismatch / truncated);
5 write failure. Errors go to stderr.

## Features (stable ids)
`pecli.file_roundtrip`, `pecli.add_get_list`, `pecli.update_remove`, `pecli.checksum`, `pecli.error_bad_magic`,
`pecli.error_missing_file`, `pecli.error_corrupt`, `pecli.error_usage` (all Linux-observable via Wine) and
`pecli.windows_native_exec` (`observable_on: windows_only`, no expected values: console code page, CRLF through
cmd/PowerShell redirection, Windows path forms are not exercised by Wine runs).

## Build
`./build.sh` = `x86_64-w64-mingw32-gcc -O1 -s -Wall -Wextra -Wl,--no-insert-timestamp -Wl,--build-id=none` (GCC 13-win32),
mtimes set to epoch 0. Two consecutive builds give the identical sha256 (`c4b79346...c68f`).

## Oracle (how `expected/` was produced)
`python3 ../tools/scenario_runner.py generate --fixture .` runs every scenario in a fresh temp directory against
`wine original/pecli.exe` (wine-9.0, `WINEDEBUG=-all`, prefix `/opt/rebuild-tools/wineprefix`): **8 scenarios, 23 steps**.
Wine is non-certifying for Windows (DR-9).

* mingw text-mode stdout/stderr emit **CRLF**; the oracle stores the raw text and `*_normalized` (CRLF -> LF).
  Comparators must use the normalized fields (a Rust remake prints LF).
* `from_original` setup files are copied from `original/`; `flip_byte` flips one byte (used for the CRC-failure case).
* Files are compared byte-exactly via sha256, so the CRC32 and record encoding must match.

## Verifier gate controls (no wine needed)
`harness/selfcheck.sh`:
1. the C source compiled natively with `gcc` must **pass** all 8 scenarios (positive control);
2. `wrong_remake` must **fail**: it omits CRC32's final XOR (every stored CRC, file hash and `checksum` output differs),
   exits 1 instead of 3 for a missing file and 1 instead of 2 for bad magic, and prints a shorter usage text.
   Result: 0 of 8 scenarios pass; the script exits non-zero if it were ever accepted.

Candidate check against any executable: `python3 ../tools/scenario_runner.py check --fixture . --launcher "/path/to/candidate"`.
