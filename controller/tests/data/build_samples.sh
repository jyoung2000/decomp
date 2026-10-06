#!/bin/sh
# Rebuilds the tiny native samples used by test_rizin.py. Deterministic-ish flags, no debug info, symbols kept.
set -e
cd "$(dirname "$0")"
x86_64-w64-mingw32-gcc -O1 -fno-asynchronous-unwind-tables -Wl,--no-insert-timestamp -o sample_pe.exe sample.c
x86_64-w64-mingw32-strip --strip-debug sample_pe.exe
gcc -O1 -fno-asynchronous-unwind-tables -no-pie -o sample_elf sample.c
strip --strip-debug sample_elf
ls -l sample_pe.exe sample_elf
