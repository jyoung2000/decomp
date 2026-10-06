# javacli fixture (Java 17 console jar)

Tiny ledger CLI: `javacli <ledger.txt> add NAME AMOUNT | remove NAME | list | total | stats`. The ledger is a text file
(`LEDGER1` header, then `name<TAB>cents`). Source (`src/dev/rebuild/ledger/{Main,Ledger,Entry,Money}.java`, a record, a nested
exception, a lambda, a multi-catch) is ground truth for the evaluation harness only; adapters get `original/` only:
`javacli.jar`, `javacli.cmd` (launcher), `README.txt`.

## Build
`PYTHON=<python> ./build.sh` compiles with `javac --release 17 -g` (class file major 61, full debug info like a default Maven/Gradle build), then `../tools/make_jar.py` writes the jar (manifest first, sorted
entries, fixed 1980-01-01 timestamps, STORED). Output is byte-reproducible for the pinned compiler: Microsoft OpenJDK 17.0.20.1
(javac emits identical class bytes for identical source on the same JDK; a different JDK patch level may change class bytes, in which
case `fixtures/tools/make_fixtures_manifest.py --verify` reports DRIFT). Observed: with `-g:none` CFR 0.152 loses parameter names and the recovered
`Entry.java` (record with a compact constructor) no longer compiles, so the fixture keeps the debug info that real builds normally have.

## Behaviour
Exit codes: 0 ok, 1 usage / unknown command / empty name, 2 corrupt (or unreadable/unwritable) ledger with the file untouched,
3 `remove` of a missing entry, 4 bad amount (non-numeric, more than 2 decimals, `long` overflow). stdout/stderr always use LF.

## Features (stable ids)
`javacli.list_total`, `javacli.add_list`, `javacli.update_remove`, `javacli.stats`, `javacli.amount_parsing`,
`javacli.load_existing`, `javacli.error_corrupt`, `javacli.error_missing`, `javacli.error_usage` (host-observable) and
`javacli.launcher_cmd` (`windows_only`: the `.cmd` launcher is not exercised by the oracle).

## Oracle
`python ../tools/scenario_runner.py generate --fixture .` runs **11 scenarios (38 steps)** with `java -jar original/javacli.jar`
(JDK 17.0.20.1, Windows 11; `JAVA_TOOL_OPTIONS`/`_JAVA_OPTIONS`/`JDK_JAVA_OPTIONS` removed from the child environment so "Picked up"
banners cannot leak into stderr). `harness/selfcheck.sh` replays them and must match 11/11.
The recovery test in `controller/tests/test_jvm.py` additionally decompiles `javacli.jar` with CFR, recompiles the recovered
sources with `javac` and replays the same frozen scenarios against the recompiled program.
