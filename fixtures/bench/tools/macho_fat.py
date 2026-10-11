#!/usr/bin/env python3
"""Assemble a fat/universal Mach-O from thin Mach-O files (what Apple's `lipo -create` does), for the R7 bench rows.

No lipo exists on the Windows build host, so this writes the documented layout itself (<mach-o/fat.h>):
  struct fat_header { uint32 magic = 0xCAFEBABE; uint32 nfat_arch; }                 (big-endian)
  struct fat_arch   { int32 cputype; int32 cpusubtype; uint32 offset; uint32 size; uint32 align; }
Each slice starts at a multiple of 2**align (lipo uses 2**14 for arm64 and 2**12 for x86_64) and is copied byte for byte, so a
slice extracted again is identical to the thin input. Kept independent of the controller's Mach-O parser on purpose: the product's
slice extraction is tested against this writer, not against itself.

usage: python macho_fat.py OUT THIN [THIN ...]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

FAT_MAGIC = 0xCAFEBABE
CPU_X86_64, CPU_ARM64 = 0x01000007, 0x0100000C
THIN_MAGICS = {b"\xcf\xfa\xed\xfe": "<", b"\xce\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">", b"\xfe\xed\xfa\xce": ">"}


def thin_cpu(data: bytes) -> tuple[int, int]:
    end = THIN_MAGICS.get(data[:4])
    if end is None:
        raise ValueError("input is not a thin Mach-O")
    cpu, sub = struct.unpack_from(end + "ii", data, 4)
    return cpu & 0xFFFFFFFF, sub & 0xFFFFFFFF


def align_for(cpu: int) -> int:
    return 14 if cpu == CPU_ARM64 else 12


def build_fat(thins: list[bytes]) -> bytes:
    if not thins:
        raise ValueError("no slices")
    archs = []
    off = 8 + 20 * len(thins)
    for data in thins:
        cpu, sub = thin_cpu(data)
        al = align_for(cpu)
        off = (off + (1 << al) - 1) & ~((1 << al) - 1)
        archs.append((cpu, sub, off, len(data), al, data))
        off += len(data)
    if len({a[0] for a in archs}) != len(archs):
        raise ValueError("two slices with the same cpu type")
    out = bytearray(struct.pack(">II", FAT_MAGIC, len(archs)))
    for cpu, sub, o, size, al, _ in archs:
        out += struct.pack(">IIIII", cpu, sub, o, size, al)
    for _cpu, _sub, o, _size, _al, data in archs:
        out += b"\0" * (o - len(out))
        out += data
    return bytes(out)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    out = Path(argv[1])
    out.write_bytes(build_fat([Path(p).read_bytes() for p in argv[2:]]))
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(argv) - 2} slices)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
