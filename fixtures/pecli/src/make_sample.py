#!/usr/bin/env python3
"""Deterministically write sample.dat in the PCLI v1 format (independent of pecli.exe)."""
import struct, sys, zlib

records = [("name", "pecli sample"), ("owner", "fixtures"), ("level", "7")]
body = b"".join(struct.pack("<HH", len(k), len(v)) + k.encode() + v.encode() for k, v in records)
hdr = b"PCLI" + struct.pack("<III", 1, len(records), zlib.crc32(body) & 0xFFFFFFFF)
open(sys.argv[1], "wb").write(hdr + body)
