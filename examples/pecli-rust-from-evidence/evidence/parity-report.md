# Parity report — pecli

Generated 2026-10-06T12:10:37.764Z. Target: rust / exe.

**Full parity:** YES  — features: 8 total, 8 verified, 0 partial, 0 failed, 0 untested, 0 stale.
_feature counts are semantic features, not files/functions; undiscovered scope is not counted_

## Candidate r2 `cand_01a11113fa217f3ccb63d3`
build hash `e7bd2792ea0e7570`, build built, verification **verified**, last known good: True

## Features

| Feature | Origin | Critical | Implementation | Verification |
|---|---|---|---|---|
| init creates an empty store: byte-exact 16-byte file | user |  | blocked | verified |
| init, add three records, get one, list all; final file byte-exact | user |  | blocked | verified |
| replace an existing key, remove another, list; final file byte-exact | user |  | blocked | verified |
| checksum of the shipped sample.dat; file unchanged | user |  | blocked | verified |
| file with wrong magic: exit 2, message on stderr, file untouched | user |  | blocked | verified |
| missing file: exit 3; no file is created | user |  | blocked | verified |
| valid header but a flipped record byte: CRC mismatch, exit 4 | user |  | blocked | verified |
| missing key exits 1; unknown command and no args print usage and exit 1 | user |  | blocked | verified |

## Comparisons

| Feature | Channel | Rule | Verdict |
|---|---|---|---|
| pecli.file_roundtrip | exit_code | exact | pass |
| pecli.file_roundtrip | stdout | normalize:crlf | pass |
| pecli.file_roundtrip | stderr | normalize:crlf | pass |
| pecli.file_roundtrip | files | sha256:exact | pass |
| pecli.add_get_list | exit_code | exact | pass |
| pecli.add_get_list | stdout | normalize:crlf | pass |
| pecli.add_get_list | stderr | normalize:crlf | pass |
| pecli.add_get_list | exit_code | exact | pass |
| pecli.add_get_list | stdout | normalize:crlf | pass |
| pecli.add_get_list | stderr | normalize:crlf | pass |
| pecli.add_get_list | exit_code | exact | pass |
| pecli.add_get_list | stdout | normalize:crlf | pass |
| pecli.add_get_list | stderr | normalize:crlf | pass |
| pecli.add_get_list | exit_code | exact | pass |
| pecli.add_get_list | stdout | normalize:crlf | pass |
| pecli.add_get_list | stderr | normalize:crlf | pass |
| pecli.add_get_list | exit_code | exact | pass |
| pecli.add_get_list | stdout | normalize:crlf | pass |
| pecli.add_get_list | stderr | normalize:crlf | pass |
| pecli.add_get_list | exit_code | exact | pass |
| pecli.add_get_list | stdout | normalize:crlf | pass |
| pecli.add_get_list | stderr | normalize:crlf | pass |
| pecli.add_get_list | files | sha256:exact | pass |
| pecli.update_remove | exit_code | exact | pass |
| pecli.update_remove | stdout | normalize:crlf | pass |
| pecli.update_remove | stderr | normalize:crlf | pass |
| pecli.update_remove | exit_code | exact | pass |
| pecli.update_remove | stdout | normalize:crlf | pass |
| pecli.update_remove | stderr | normalize:crlf | pass |
| pecli.update_remove | exit_code | exact | pass |
| pecli.update_remove | stdout | normalize:crlf | pass |
| pecli.update_remove | stderr | normalize:crlf | pass |
| pecli.update_remove | files | sha256:exact | pass |
| pecli.checksum | exit_code | exact | pass |
| pecli.checksum | stdout | normalize:crlf | pass |
| pecli.checksum | stderr | normalize:crlf | pass |
| pecli.checksum | exit_code | exact | pass |
| pecli.checksum | stdout | normalize:crlf | pass |
| pecli.checksum | stderr | normalize:crlf | pass |
| pecli.checksum | files | sha256:exact | pass |
| pecli.error_bad_magic | exit_code | exact | pass |
| pecli.error_bad_magic | stdout | normalize:crlf | pass |
| pecli.error_bad_magic | stderr | normalize:crlf | pass |
| pecli.error_bad_magic | exit_code | exact | pass |
| pecli.error_bad_magic | stdout | normalize:crlf | pass |
| pecli.error_bad_magic | stderr | normalize:crlf | pass |
| pecli.error_bad_magic | files | sha256:exact | pass |
| pecli.error_missing_file | exit_code | exact | pass |
| pecli.error_missing_file | stdout | normalize:crlf | pass |
| pecli.error_missing_file | stderr | normalize:crlf | pass |
| pecli.error_missing_file | exit_code | exact | pass |
| pecli.error_missing_file | stdout | normalize:crlf | pass |
| pecli.error_missing_file | stderr | normalize:crlf | pass |
| pecli.error_missing_file | exit_code | exact | pass |
| pecli.error_missing_file | stdout | normalize:crlf | pass |
| pecli.error_missing_file | stderr | normalize:crlf | pass |
| pecli.error_missing_file | files | sha256:exact | pass |
| pecli.error_corrupt | exit_code | exact | pass |
| pecli.error_corrupt | stdout | normalize:crlf | pass |
| pecli.error_corrupt | stderr | normalize:crlf | pass |
| pecli.error_corrupt | exit_code | exact | pass |
| pecli.error_corrupt | stdout | normalize:crlf | pass |
| pecli.error_corrupt | stderr | normalize:crlf | pass |
| pecli.error_corrupt | files | sha256:exact | pass |
| pecli.error_usage | exit_code | exact | pass |
| pecli.error_usage | stdout | normalize:crlf | pass |
| pecli.error_usage | stderr | normalize:crlf | pass |
| pecli.error_usage | exit_code | exact | pass |
| pecli.error_usage | stdout | normalize:crlf | pass |
| pecli.error_usage | stderr | normalize:crlf | pass |
| pecli.error_usage | exit_code | exact | pass |
| pecli.error_usage | stdout | normalize:crlf | pass |
| pecli.error_usage | stderr | normalize:crlf | pass |
| pecli.error_usage | exit_code | exact | pass |
| pecli.error_usage | stdout | normalize:crlf | pass |
| pecli.error_usage | stderr | normalize:crlf | pass |
| pecli.error_usage | files | sha256:exact | pass |

Environment: Linux 6.18.44-fc-v70 x86_64, wine=True, host_certifies_windows=False

## Unresolved
- init creates an empty store: byte-exact 16-byte file: impl blocked, verify verified
- init, add three records, get one, list all; final file byte-exact: impl blocked, verify verified
- replace an existing key, remove another, list; final file byte-exact: impl blocked, verify verified
- checksum of the shipped sample.dat; file unchanged: impl blocked, verify verified
- file with wrong magic: exit 2, message on stderr, file untouched: impl blocked, verify verified
- missing file: exit 3; no file is created: impl blocked, verify verified
- valid header but a flipped record byte: CRC mismatch, exit 4: impl blocked, verify verified
- missing key exits 1; unknown command and no args print usage and exit 1: impl blocked, verify verified

## AI usage
- no AI calls were made for this case

## Reproduction

```
rebuildctl rebuild --source "/home/user/decomp/fixtures/pecli/original" --output "/tmp/claude-0/-home-user-decomp/c456138f-a927-5bb9-bf2d-fa1845ce6ed1/scratchpad/pecli-demo/out" --language rust --type exe
```
