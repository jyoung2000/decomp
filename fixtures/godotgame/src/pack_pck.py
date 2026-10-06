#!/usr/bin/env python3
"""Pure-Python writer (and reader/verifier) for the Godot 4 PCK container, pack format version 2.

Layout, little endian, as written by Godot 4.3 `PCKPacker` (core/io/pck_packer.cpp):

  u32  magic            0x43504447  ("GDPC")
  u32  pack_format      2
  u32  godot_major, godot_minor, godot_patch
  u32  pack_flags       bit0 = encrypted directory (unused here), bit1 = file base is relative to pck start
                        (0 for engine 4.3 as emitted here; 2 for >= 4.4)
  u64  file_base        absolute offset of the file-data area (offsets below are relative to it)
  u32  reserved[16]     all zero
  u32  file_count
  per file:
    u32  path_len       length of the path INCLUDING zero padding to a multiple of 4
    u8   path[path_len] utf-8 "res://..." padded with 0x00
    u64  offset         relative to file_base
    u64  size
    u8   md5[16]
    u32  flags          bit0 = encrypted file (unused)
  zero padding up to `alignment` (32), then the file data area; each file is padded with zeros
  to the next `alignment` boundary.

Deterministic: files are added in sorted order; no timestamps are stored.

usage:
  pack_pck.py pack   <project_dir> <out.pck> [--godot 4.3.0] [--align 32]
  pack_pck.py verify <pck> [<project_dir>]   # parses the pck; with a dir, diffs every entry against it
"""
import hashlib, os, struct, sys

MAGIC = 0x43504447
PACK_FORMAT = 2
PACK_REL_FILEBASE = 2
EXCLUDE_NAMES = {"pack_pck.py", "make_wav.py"}   # build tooling, not game content
EXCLUDE_DIRS = {".godot", ".git", "__pycache__"}


def pad(align, n):
    r = n % align
    return align - r if r else 0


def collect(project_dir):
    out = []
    for dp, dns, fns in os.walk(project_dir):
        dns[:] = sorted(d for d in dns if d not in EXCLUDE_DIRS)
        for fn in sorted(fns):
            if fn in EXCLUDE_NAMES and dp == project_dir:
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, project_dir).replace(os.sep, "/")
            out.append(("res://" + rel, full))
    out.sort(key=lambda e: e[0])
    return out


def pack(project_dir, out_path, version=(4, 3, 0), align=32):
    entries = collect(project_dir)
    blobs = []
    ofs = 0
    table = []
    for path, full in entries:
        data = open(full, "rb").read()
        table.append((path, ofs, len(data), hashlib.md5(data).digest()))
        blobs.append(data + b"\0" * pad(align, len(data)))
        ofs += len(blobs[-1])
    # Godot 4.3 writes flags=0 (verified against GDRE `--pck-create --pck-version=2 --pck-engine-version=4.3.0`);
    # 4.4+ sets PACK_REL_FILEBASE.
    flags = PACK_REL_FILEBASE if tuple(version) >= (4, 4, 0) else 0
    head = struct.pack("<IIIIII", MAGIC, PACK_FORMAT, *version, flags)
    dir_bytes = struct.pack("<I", len(table))
    for path, o, size, md5 in table:
        pb = path.encode("utf-8")
        pb += b"\0" * pad(4, len(pb))
        dir_bytes += struct.pack("<I", len(pb)) + pb + struct.pack("<QQ", o, size) + md5 + struct.pack("<I", 0)
    fixed = len(head) + 8 + 16 * 4 + len(dir_bytes)
    file_base = fixed + pad(align, fixed)
    blob = head + struct.pack("<Q", file_base) + b"\0" * (16 * 4) + dir_bytes
    blob += b"\0" * (file_base - len(blob)) + b"".join(blobs)
    with open(out_path, "wb") as f:
        f.write(blob)
    return table


def read(pck_path):
    d = open(pck_path, "rb").read()
    magic, fmt, maj, mnr, pat, flags = struct.unpack_from("<IIIIII", d, 0)
    assert magic == MAGIC, "bad magic"
    assert fmt == 2, f"unsupported pack format {fmt}"
    (file_base,) = struct.unpack_from("<Q", d, 24)
    assert struct.unpack_from("<16I", d, 32) == (0,) * 16, "reserved ints not zero"
    (count,) = struct.unpack_from("<I", d, 96)
    pos, files = 100, []
    for _ in range(count):
        (pl,) = struct.unpack_from("<I", d, pos); pos += 4
        path = d[pos:pos + pl].rstrip(b"\0").decode("utf-8"); pos += pl
        o, size = struct.unpack_from("<QQ", d, pos); pos += 16
        md5 = d[pos:pos + 16]; pos += 16
        (fl,) = struct.unpack_from("<I", d, pos); pos += 4
        data = d[file_base + o:file_base + o + size]
        assert hashlib.md5(data).digest() == md5, f"md5 mismatch for {path}"
        files.append({"path": path, "offset": o, "size": size, "md5": md5.hex(), "flags": fl, "data": data})
    return {"format": fmt, "version": (maj, mnr, pat), "flags": flags, "file_base": file_base, "files": files}


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "pack":
        version, align = (4, 3, 0), 32
        if "--godot" in a:
            version = tuple(int(x) for x in a[a.index("--godot") + 1].split("."))
        if "--align" in a:
            align = int(a[a.index("--align") + 1])
        t = pack(a[1], a[2], version, align)
        print(f"packed {len(t)} files -> {a[2]} ({os.path.getsize(a[2])} bytes)")
    elif len(a) >= 2 and a[0] == "verify":
        r = read(a[1])
        bad = 0
        if len(a) > 2:
            for f in r["files"]:
                src = os.path.join(a[2], f["path"][len("res://"):])
                if not os.path.isfile(src) or open(src, "rb").read() != f["data"]:
                    print("MISMATCH", f["path"]); bad += 1
        print(f"format={r['format']} godot={r['version']} flags={r['flags']} files={len(r['files'])} mismatches={bad}")
        sys.exit(1 if bad else 0)
    else:
        print(__doc__); sys.exit(2)


if __name__ == "__main__":
    main()
