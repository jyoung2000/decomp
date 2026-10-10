"""ConfuserEx-style identifier renaming for a managed assembly, done in place in the #Strings heap (our own tool, no download).

Every user-defined identifier string (type names, namespaces, method/field/param/property/event names) is overwritten with a
deterministic same-length gibberish name, so no metadata table, offset or heap size changes and the assembly still loads.
Renaming is per heap entry, so all rows sharing one string (an interface method and its implementations) stay consistent.

Skipped, and reported, so the renamed assembly keeps working:
* strings also referenced by TypeRef/MemberRef/AssemblyRef/... (names that bind to other assemblies, e.g. ToString overrides);
* special names (".ctor", ".cctor"), compiler-generated names ("<...>"), "value__" and enum members (Enum.Parse uses them);
* names that are a suffix of an external name; a string that shares its tail with a shorter candidate is renamed through that
  tail (only the shared suffix bytes change, e.g. "get_Priority" -> "get_<new>"), and names too short to rename uniquely.

usage: dotnet_rename.py <in.dll> <out.dll> [--map map.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "controller"))
from rebuild_controller.backends.ilspy import _S, _SCHEMA, _Tables  # noqa: E402  (reuse the controller's metadata reader)

ALPHA = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
EXTERNAL_TABLES = {0x00, 0x01, 0x0A, 0x1A, 0x1C, 0x20, 0x23, 0x26, 0x27, 0x28}   # Module, TypeRef, MemberRef, ModuleRef, ImplMap, Assembly(Ref), File, ExportedType, ManifestResource


def _metadata_file_offset(data: bytes) -> tuple[int, int]:
    import pefile
    pe = pefile.PE(data=data, fast_load=True)
    dd = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14]
    clr = pe.get_data(dd.VirtualAddress, 72)
    md_rva, md_size = struct.unpack_from("<II", clr, 8)
    off = pe.get_offset_from_rva(md_rva)
    pe.close()
    return off, md_size


def _new_name(index: int, length: int) -> str | None:
    """Unique per index; None when `length` characters cannot hold a unique name."""
    digits = []
    n = index
    while True:
        digits.append(ALPHA[n % len(ALPHA)])
        n //= len(ALPHA)
        if n == 0:
            break
    core = "".join(reversed(digits))
    if len(core) > length:
        return None
    pad = hashlib.sha256(f"{index}".encode()).hexdigest()
    filler = "".join(ALPHA[int(pad[i:i + 2], 16) % len(ALPHA)] for i in range(0, 2 * (length - len(core)), 2))
    return filler + core


def rename(src: Path, dst: Path) -> dict:
    data = bytearray(src.read_bytes())
    md_off, md_size = _metadata_file_offset(bytes(data))
    md = bytes(data[md_off:md_off + md_size])
    t = _Tables(md)
    heap_base, heap_size = t.strings

    def str_cols(table: int) -> list[int]:
        return [i for i, c in enumerate(_SCHEMA[table]) if c == _S]

    referenced: set[int] = set()
    external: set[int] = set()
    for table in t.rows:
        if table not in _SCHEMA:
            continue
        cols = str_cols(table)
        for r in range(1, t.rows[table] + 1):
            row = t.row(table, r)
            for c in cols:
                if row[c]:
                    referenced.add(row[c])
                    if table in EXTERNAL_TABLES:
                        external.add(row[c])
    # enum types: TypeDefs whose base is System.Enum; their fields keep names
    n_types = t.rows.get(0x02, 0)
    n_fields = t.rows.get(0x04, 0)
    enum_fields: set[int] = set()
    for i in range(1, n_types + 1):
        row = t.row(0x02, i)
        tab, idx = t.coded("TypeDefOrRef", row[3])
        base = ""
        if tab == 0x01 and idx:
            rr = t.row(0x01, idx)
            base = f"{t.string(rr[2])}.{t.string(rr[1])}"
        if base == "System.Enum":
            first = row[4]
            last = t.row(0x02, i + 1)[4] if i < n_types else n_fields + 1
            enum_fields.update(range(first, last))
    candidates: dict[int, str] = {}
    targets = [(0x02, 1), (0x02, 2), (0x06, 3), (0x04, 1), (0x08, 2), (0x17, 1), (0x14, 1)]
    for table, col in targets:
        for r in range(1, t.rows.get(table, 0) + 1):
            if table == 0x04 and r in enum_fields:
                continue
            off = t.row(table, r)[col]
            if off:
                candidates[off] = t.string(off)
    ends = {o: o + len(t.string(o).encode("utf-8")) for o in referenced}
    renamed, skipped = {}, {}
    index = 0
    done: set[int] = set()   # offsets whose bytes were rewritten
    for off in sorted(candidates, reverse=True):   # inner suffixes (higher offsets) first
        name = candidates[off]
        end = ends[off]
        why = None
        if off in external:
            why = "also names an external reference"
        elif not name or name.startswith((".", "<")) or name == "value__" or name == "<Module>":
            why = "special or compiler-generated name"
        elif any(off < o < end for o in referenced):
            why = None if any(off < o < end and o in done for o in referenced) else "heap entry shared with another string (suffix sharing)"
            if why is None:
                renamed[name] = "(renamed through its shared suffix)"
                continue
        elif any(o < off < e and o in external for o, e in ends.items()):
            why = "suffix of an external reference name"
        if why is None:
            nn = _new_name(index, len(name.encode("utf-8")))
            if nn is None:
                why = "too short for a unique replacement"
            else:
                index += 1
                pos = md_off + heap_base + off
                assert data[pos:pos + len(nn)] == name.encode("utf-8")
                data[pos:pos + len(nn)] = nn.encode("ascii")
                renamed[name] = nn
                done.add(off)
                continue
        skipped[name] = why
    dst.write_bytes(bytes(data))
    return {"renamed": renamed, "skipped": skipped, "renamed_count": len(renamed), "skipped_count": len(skipped)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--map", type=Path)
    a = ap.parse_args()
    rep = rename(a.src, a.dst)
    if a.map:
        a.map.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"renamed {rep['renamed_count']} identifiers, kept {rep['skipped_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
