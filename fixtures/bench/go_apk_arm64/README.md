# Benchmark row `go_apk_arm64`

unsigned APK (build-tools 34 aapt2/d8/zipalign) whose native library lib/arm64-v8a/libbenchgo.so is benchgo built for android/arm64 (position-independent ELF, stripped); no NDK on the host, so it is a helper executable in the library slot (run by MainActivity), not a JNI library.

Ground truth for the scoreboard (`scripts/benchmark.py`, R7 input kinds). The analyzer only ever sees `bin/`; `truth/` and the sources are for scoring.

| Item | Value |
|---|---|
| Analysis input | `bin/benchapk.apk` (2826775 bytes, sha256 `37f990801d312319198af574e2b401a872c06b2a968c1cc58331638148ea5b83`) |
| Scored member | `lib/arm64-v8a/libbenchgo.so` (2818306 bytes, sha256 `0633fa1e344dfc07f888502caadc13cd5031e273c8d056d1ef8bec70d64af6a7`) |
| Source | `fixtures/bench/go_pe_x64/src` (Go) + `fixtures/bench/go_apk_arm64/src` (manifest, Java) |
| Toolchain | go version go1.27.0 windows/amd64; javac 17.0.20.1; Android build-tools 34.0.0 (aapt2, d8, zipalign), android-34 |
| Stripped/obfuscated how | -ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary); APK unsigned |
| Truth from | go tool nm -size -sort address on the unstripped build (identical .text, verified) |
| Truth size | 2666 functions (9 from our source), 0 imports, 6 notable strings |

No Android NDK and no C toolchain exist on the build host, and Go can only build `android/arm64` without cgo, so the native library is our Go program as a position-independent ELF executable placed in the library slot (`lib/arm64-v8a/libbenchgo.so`, the pattern apps use for helper executables; `MainActivity` runs it from `nativeLibraryDir`). It has no JNI exports. Other ABIs (armeabi-v7a, x86, x86_64) need cgo + an NDK and are not built. The APK is unsigned and was never installed on a device. The scoreboard extracts the library with the product's own member extraction and scores it (`go_apk_arm64 [lib/arm64-v8a/libbenchgo.so]`).

## Recorded build

```
GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -buildid=' -o libbenchgo.so .
(truth build) go build -trimpath -buildvcs=false -ldflags=-buildid= -o sym_libbenchgo.so .
javac --release 8 -g:none -encoding UTF-8 -cp android.jar java/**/*.java
d8 --release --min-api 24 --lib android.jar *.class
aapt2 link --manifest AndroidManifest.xml -I android.jar -o base.apk
zip (stored, timestamps 2026-01-01): base.apk entries + classes.dex + lib/arm64-v8a/libbenchgo.so
zipalign -f -p 4 unaligned.apk benchapk.apk
```

Rebuild: `python fixtures/bench/build_bench.py --rows go_apk_arm64`; check reproducibility with `python fixtures/bench/build_bench.py --verify --rows go_apk_arm64`.
