"""Minimal, dependency-free PDB (MSF 7.00) reader for benchmark ground truth.

Reads only what R0 needs from a PDB written by the MSVC linker (also rustc's msvc target):
* procedure symbols (S_GPROC32 / S_LPROC32 and their _ID variants) from every module stream: name, section:offset, code size,
  and the module (object file) they came from;
* public symbols (S_PUB32) flagged as functions, for linked code that has no private procedure record (e.g. import thunks).

It is deliberately independent of rizin so the truth does not come from the analyzer being measured.
Format reference: LLVM's "The PDB File Format" docs (MSF superblock, stream directory, DBI module info, CodeView records).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
S_PUB32 = 0x110E
S_LPROC32 = 0x110F
S_GPROC32 = 0x1110
S_LPROC32_ID = 0x1146
S_GPROC32_ID = 0x1147
PROC_KINDS = {S_LPROC32, S_GPROC32, S_LPROC32_ID, S_GPROC32_ID}
CVPSF_CODE = 0x1
CVPSF_FUNCTION = 0x2


class PdbError(ValueError):
    pass


@dataclass
class Proc:
    name: str
    segment: int
    offset: int
    size: int
    module: str
    kind: str        # "global" | "local"


@dataclass
class Public:
    name: str
    segment: int
    offset: int
    is_function: bool


class Msf:
    def __init__(self, data: bytes):
        if not data.startswith(MSF_MAGIC):
            raise PdbError("not an MSF 7.00 PDB")
        self.data = data
        self.block_size, _fpm, self.num_blocks, dir_bytes, _unk, block_map_addr = struct.unpack_from("<6I", data, 32)
        if self.block_size not in (512, 1024, 2048, 4096):
            raise PdbError(f"bad block size {self.block_size}")
        n_dir_blocks = -(-dir_bytes // self.block_size)
        dir_block_idx = struct.unpack_from(f"<{n_dir_blocks}I", data, block_map_addr * self.block_size)
        directory = b"".join(self._block(i) for i in dir_block_idx)[:dir_bytes]
        n_streams = struct.unpack_from("<I", directory, 0)[0]
        sizes = struct.unpack_from(f"<{n_streams}I", directory, 4)
        pos = 4 + 4 * n_streams
        self.streams: list[tuple[int, list[int]]] = []
        for size in sizes:
            if size == 0xFFFFFFFF:
                self.streams.append((0, []))
                continue
            nb = -(-size // self.block_size)
            blocks = list(struct.unpack_from(f"<{nb}I", directory, pos))
            pos += 4 * nb
            self.streams.append((size, blocks))

    def _block(self, i: int) -> bytes:
        return self.data[i * self.block_size:(i + 1) * self.block_size]

    def stream(self, idx: int) -> bytes:
        if idx >= len(self.streams):
            raise PdbError(f"stream {idx} out of range")
        size, blocks = self.streams[idx]
        return b"".join(self._block(b) for b in blocks)[:size]


def _cstr(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _records(buf: bytes, start: int, end: int):
    pos = start
    while pos + 4 <= end:
        reclen, kind = struct.unpack_from("<HH", buf, pos)
        if reclen < 2:
            break
        yield kind, buf[pos + 4:pos + 2 + reclen]
        pos += 2 + reclen


def read_pdb(path: Path | str) -> dict:
    msf = Msf(Path(path).read_bytes())
    dbi = msf.stream(3)
    if len(dbi) < 64:
        raise PdbError("DBI stream too short")
    (_sig, _ver, _age, _gsi, _build, _psi, _dllver, sym_rec_stream, _rbld, mod_info_size, _sc_size, _secmap_size,
     _srcinfo_size, _tsmap_size, _mfc, _optdbg_size, _ec_size, _flags, machine, _pad) = struct.unpack_from("<iIIHHHHHHiiiiiIiiHHI", dbi, 0)
    procs: list[Proc] = []
    pos, end = 64, 64 + mod_info_size
    while pos < end:
        mod_stream, sym_bytes = struct.unpack_from("<HI", dbi, pos + 34)
        mod_name, p = _cstr(dbi, pos + 64)
        _obj_name, p = _cstr(dbi, p)
        pos = (p + 3) & ~3
        if mod_stream == 0xFFFF or sym_bytes <= 4:
            continue
        ms = msf.stream(mod_stream)
        for kind, rec in _records(ms, 4, min(sym_bytes, len(ms))):
            if kind in PROC_KINDS and len(rec) >= 35:
                _parent, _end, _next, code_size, _ds, _de, _ftype, off, seg, _fl = struct.unpack_from("<IIIIIIIIHB", rec, 0)
                name, _ = _cstr(rec, 35)
                procs.append(Proc(name, seg, off, code_size, Path(mod_name.replace("\\", "/")).name,
                                  "global" if kind in (S_GPROC32, S_GPROC32_ID) else "local"))
    publics: list[Public] = []
    if sym_rec_stream != 0xFFFF:
        sr = msf.stream(sym_rec_stream)
        for kind, rec in _records(sr, 0, len(sr)):
            if kind == S_PUB32 and len(rec) >= 10:
                flags, off, seg = struct.unpack_from("<IIH", rec, 0)
                name, _ = _cstr(rec, 10)
                publics.append(Public(name, seg, off, bool(flags & (CVPSF_CODE | CVPSF_FUNCTION))))
    return {"machine": machine, "procs": procs, "publics": publics}
