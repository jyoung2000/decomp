"""ILSpy (ilspycmd) backend for managed .NET code, plus a native ECMA-335 metadata reader.

What this backend genuinely does
- ``metadata``: reads assembly identity, references, target framework and the type table straight from the PE/CLR
  metadata (pure Python, no tool needed) and cross-checks the type list against ``ilspycmd -l``.
- ``decompile``: runs ``ilspycmd -p -o <dir>`` (one C# file per type + .csproj). When the whole-project run aborts (ILSpy
  throws on a method it cannot read) it falls back to one ``ilspycmd -t <type>`` run per top-level type so one bad
  method does not hide the rest. A ``recovery_report`` counts types that decompiled cleanly, with ``/*Error near IL_...*/``
  markers, or not at all. It never claims the C# is equivalent to the original source.
- Unity: Mono (Assembly-CSharp*.dll + UnityEngine refs) is decompiled like any assembly and labelled ``unity_mono``.
  IL2CPP (GameAssembly + global-metadata.dat) is reported as ``unity_il2cpp`` experimental/unsupported: native code is
  not decompiled and no recovery is faked.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path
from typing import Any

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Settings, get_settings
from ..ids import sha256_file
from ..jobs.runner import StageError
from ..paths import PathPolicyError, assert_output_not_in_source, is_within, resolve_final
from .archive import (RecoveryToolProbe, cap_list, discover_executable, probe_error, record_evidence, resolve_studio,
                      run_bounded, sha256_of)

BACKEND_ID = "ilspy"
TOOL_NAME = "ilspycmd"
PINNED_VERSION = "9.1.0.7988"
LICENSE = "MIT"
SOURCE_URL = "https://github.com/icsharpcode/ILSpy"
INSTALL_HINT = f"dotnet tool install ilspycmd --version {PINNED_VERSION} --tool-path ~/.dotnet/tools   (requires the .NET 8 SDK/runtime)"
PREREQUISITES = [".NET 8 runtime"]
SCHEMA_VERSION = 1

LIST_CAP = 2000       # types/files carried in evidence bodies
FALLBACK_MAX_TYPES = 400
FALLBACK_BUDGET_S = 900

# Real marker texts produced by ILSpy 9.1 (verified against deliberately corrupted IL, see tests).
_IL_ERROR_RE = re.compile(r"/\*Error near IL_[0-9A-Fa-f]+[^*]*\*/")
_COMMENT_ERROR_RE = re.compile(r"^\s*//\s*Error\b", re.M)
_UNKNOWN_RESULT_RE = re.compile(r"Unknown result type \(might be due to invalid IL or missing references\)")

# ILSpy writes 'Error decompiling @06000002 Rebuild.Sample.Point.Sum' when a method body cannot be read at all.
_DECOMPILE_FAILURE_RE = re.compile(r"Error decompiling @([0-9A-Fa-f]{8}) ([^\s)]+)")

_SMOKE_DLL_B64 = (
    "TVqQAAMAAAAEAAAA//8AALgAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAgAAAAA4fug4AtAnNIbgBTM0hVGhpcyBwcm9ncmFtIGNhbm5vdCBiZSBydW4gaW4gRE9TIG1v"
    "ZGUuDQ0KJAAAAAAAAABQRQAATAEDAN4m+54AAAAAAAAAAOAAIiALATAAAAQAAAAGAAAAAAAA6iMA"
    "AAAgAAAAQAAAAAAAEAAgAAAAAgAABAAAAAAAAAAEAAAAAAAAAACAAAAAAgAAAAAAAAMAYIUAABAA"
    "ABAAAAAAEAAAEAAAAAAAABAAAAAAAAAAAAAAAJgjAABPAAAAAEAAALACAAAAAAAAAAAAAAAAAAAA"
    "AAAAAGAAAAwAAAB8IwAAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAIAAACAAAAAAAAAAAAAAACCAAAEgAAAAAAAAAAAAAAC50ZXh0AAAA8AMAAAAgAAAABAAAAAIA"
    "AAAAAAAAAAAAAAAAACAAAGAucnNyYwAAALACAAAAQAAAAAQAAAAGAAAAAAAAAAAAAAAAAABAAABA"
    "LnJlbG9jAAAMAAAAAGAAAAACAAAACgAAAAAAAAAAAAAAAAAAQAAAQgAAAAAAAAAAAAAAAAAAAADM"
    "IwAAAAAAAEgAAAACAAUAVCAAACgDAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4fKipCU0pCAQABAAAAAAAMAAAAdjQuMC4zMDMxOQAAAAAF"
    "AGwAAADsAAAAI34AAFgBAAAgAQAAI1N0cmluZ3MAAAAAeAIAAAQAAAAjVVMAfAIAABAAAAAjR1VJ"
    "RAAAAIwCAACcAAAAI0Jsb2IAAAAAAAAAAgAAAUcUAAAJAAAAAPoBMwAWAAABAAAABgAAAAIAAAAB"
    "AAAABAAAAAQAAAABAAAAAQAAAAAAtAABAAAAAAAGAFwA6gAGAHwA6gAGAC8A1wAPAAoBAAAGAEMA"
    "mgAGABkBwwAAAAAAAQAAAAAAAQABAIEBEAAbACEAGQABAAEAUCAAAAAAlgDKAB4AAQAJANEAAQAR"
    "ANEABgAZANEACgApANEAEAAuAAsAIgAuABMAKwAuABsASgAuACMAUwAEgAAAAAAAAAAAAAAAAAAA"
    "AAAWAAAAAgAAAAAAAAAAAAAAFQAKAAAAAAAAAAA8TW9kdWxlPgBuZXRzdGFuZGFyZABTbW9rZVBy"
    "b2JlAFJlYnVpbGQuU21va2UARGVidWdnYWJsZUF0dHJpYnV0ZQBUYXJnZXRGcmFtZXdvcmtBdHRy"
    "aWJ1dGUAQ29tcGlsYXRpb25SZWxheGF0aW9uc0F0dHJpYnV0ZQBSdW50aW1lQ29tcGF0aWJpbGl0"
    "eUF0dHJpYnV0ZQBTeXN0ZW0uUnVudGltZS5WZXJzaW9uaW5nAFNtb2tlUHJvYmUuZGxsAFN5c3Rl"
    "bQBBbnN3ZXIALmN0b3IAU3lzdGVtLkRpYWdub3N0aWNzAFN5c3RlbS5SdW50aW1lLkNvbXBpbGVy"
    "U2VydmljZXMARGVidWdnaW5nTW9kZXMAT2JqZWN0AAAAAABaDIytdfRnSJvf+ytSqSKEAAQgAQEI"
    "AyAAAQUgAQEREQQgAQEOCMx7E//NLd1RAwAACAgBAAgAAAAAAB4BAAEAVAIWV3JhcE5vbkV4Y2Vw"
    "dGlvblRocm93cwEIAQACAAAAAABHAQAZLk5FVFN0YW5kYXJkLFZlcnNpb249djIuMAEAVA4URnJh"
    "bWV3b3JrRGlzcGxheU5hbWURLk5FVCBTdGFuZGFyZCAyLjAAAAAAAAAAAAAAAAAAEAAAAAAAAAAA"
    "AAAAAAAAAMAjAAAAAAAAAAAAANojAAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAADMIwAAAAAAAAAA"
    "AAAAAF9Db3JEbGxNYWluAG1zY29yZWUuZGxsAAAAAAD/JQAgABAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAABABAAAAAYAACAAAAAAAAAAAAAAAAAAAABAAEAAAAwAACAAAAAAAAAAAAAAAAA"
    "AAABAAAAAABIAAAAWEAAAFQCAAAAAAAAAAAAAFQCNAAAAFYAUwBfAFYARQBSAFMASQBPAE4AXwBJ"
    "AE4ARgBPAAAAAAC9BO/+AAABAAAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAQAAAACAAAAAAAAAAAA"
    "AAAAAAAARAAAAAEAVgBhAHIARgBpAGwAZQBJAG4AZgBvAAAAAAAkAAQAAABUAHIAYQBuAHMAbABh"
    "AHQAaQBvAG4AAAAAAAAAsAS0AQAAAQBTAHQAcgBpAG4AZwBGAGkAbABlAEkAbgBmAG8AAACQAQAA"
    "AQAwADAAMAAwADAANABiADAAAAAsAAIAAQBGAGkAbABlAEQAZQBzAGMAcgBpAHAAdABpAG8AbgAA"
    "AAAAIAAAADAACAABAEYAaQBsAGUAVgBlAHIAcwBpAG8AbgAAAAAAMAAuADAALgAwAC4AMAAAAD4A"
    "DwABAEkAbgB0AGUAcgBuAGEAbABOAGEAbQBlAAAAUwBtAG8AawBlAFAAcgBvAGIAZQAuAGQAbABs"
    "AAAAAAAoAAIAAQBMAGUAZwBhAGwAQwBvAHAAeQByAGkAZwBoAHQAAAAgAAAARgAPAAEATwByAGkA"
    "ZwBpAG4AYQBsAEYAaQBsAGUAbgBhAG0AZQAAAFMAbQBvAGsAZQBQAHIAbwBiAGUALgBkAGwAbAAA"
    "AAAANAAIAAEAUAByAG8AZAB1AGMAdABWAGUAcgBzAGkAbwBuAAAAMAAuADAALgAwAC4AMAAAADgA"
    "CAABAEEAcwBzAGUAbQBiAGwAeQAgAFYAZQByAHMAaQBvAG4AAAAwAC4AMAAuADAALgAwAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAM"
    "AAAA7DMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


# =====================================================================================================================
# native ECMA-335 metadata reader
# =====================================================================================================================
class MetadataError(Exception):
    pass


_S, _G, _B = "str", "guid", "blob"
_CODED: dict[str, tuple[int, list[int | None]]] = {
    "TypeDefOrRef": (2, [0x02, 0x01, 0x1B]),
    "HasConstant": (2, [0x04, 0x08, 0x17]),
    "HasCustomAttribute": (5, [0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00, 0x0E, 0x17, 0x14, 0x11, 0x1A, 0x1B,
                               0x20, 0x23, 0x26, 0x27, 0x28, 0x2A, 0x2C, 0x2B]),
    "HasFieldMarshal": (1, [0x04, 0x08]),
    "HasDeclSecurity": (2, [0x02, 0x06, 0x20]),
    "MemberRefParent": (3, [0x02, 0x01, 0x1A, 0x06, 0x1B]),
    "HasSemantics": (1, [0x14, 0x17]),
    "MethodDefOrRef": (1, [0x06, 0x0A]),
    "MemberForwarded": (1, [0x04, 0x06]),
    "Implementation": (2, [0x26, 0x23, 0x27]),
    "CustomAttributeType": (3, [None, None, 0x06, 0x0A, None]),
    "ResolutionScope": (2, [0x00, 0x1A, 0x23, 0x01]),
    "TypeOrMethodDef": (1, [0x02, 0x06]),
}


def _c(name: str) -> tuple[str, str]:
    return ("c", name)


def _t(table: int) -> tuple[str, int]:
    return ("t", table)


_SCHEMA: dict[int, list[Any]] = {
    0x00: [2, _S, _G, _G, _G], 0x01: [_c("ResolutionScope"), _S, _S],
    0x02: [4, _S, _S, _c("TypeDefOrRef"), _t(0x04), _t(0x06)], 0x03: [_t(0x04)], 0x04: [2, _S, _B], 0x05: [_t(0x06)],
    0x06: [4, 2, 2, _S, _B, _t(0x08)], 0x07: [_t(0x08)], 0x08: [2, 2, _S], 0x09: [_t(0x02), _c("TypeDefOrRef")],
    0x0A: [_c("MemberRefParent"), _S, _B], 0x0B: [1, 1, _c("HasConstant"), _B],
    0x0C: [_c("HasCustomAttribute"), _c("CustomAttributeType"), _B], 0x0D: [_c("HasFieldMarshal"), _B],
    0x0E: [2, _c("HasDeclSecurity"), _B], 0x0F: [2, 4, _t(0x02)], 0x10: [4, _t(0x04)], 0x11: [_B],
    0x12: [_t(0x02), _t(0x14)], 0x13: [_t(0x14)], 0x14: [2, _S, _c("TypeDefOrRef")], 0x15: [_t(0x02), _t(0x17)],
    0x16: [_t(0x17)], 0x17: [2, _S, _B], 0x18: [2, _t(0x06), _c("HasSemantics")],
    0x19: [_t(0x02), _c("MethodDefOrRef"), _c("MethodDefOrRef")], 0x1A: [_S], 0x1B: [_B],
    0x1C: [2, _c("MemberForwarded"), _S, _t(0x1A)], 0x1D: [4, _t(0x04)], 0x1E: [4, 4], 0x1F: [4],
    0x20: [4, 2, 2, 2, 2, 4, _B, _S, _S], 0x21: [4], 0x22: [4, 4, 4], 0x23: [2, 2, 2, 2, 4, _B, _S, _S, _B],
    0x24: [4, _t(0x23)], 0x25: [4, 4, 4, _t(0x23)], 0x26: [4, _S, _B], 0x27: [4, 4, _S, _S, _c("Implementation")],
    0x28: [4, 4, _S, _c("Implementation")], 0x29: [_t(0x02), _t(0x02)], 0x2A: [2, 2, _c("TypeOrMethodDef"), _S],
    0x2B: [_c("MethodDefOrRef"), _B], 0x2C: [_t(0x2A), _c("TypeDefOrRef")],
}

_TFM_PREFIXES = (".NETCoreApp,Version=", ".NETFramework,Version=", ".NETStandard,Version=", ".NETPortable,", "MonoAndroid", "Xamarin")


class _Tables:
    def __init__(self, md: bytes):
        self.md = md
        if len(md) < 20 or md[:4] != b"BSJB":
            raise MetadataError("metadata root signature (BSJB) not found")
        vlen = struct.unpack_from("<I", md, 12)[0]
        if vlen > 255 or 16 + vlen + 4 > len(md):
            raise MetadataError("metadata version string length out of range")
        self.version = md[16:16 + vlen].split(b"\0")[0].decode("ascii", "replace")
        pos = 16 + vlen + 2
        nstreams = struct.unpack_from("<H", md, pos)[0]
        pos += 2
        self.streams: dict[str, tuple[int, int]] = {}
        for _ in range(nstreams):
            if pos + 8 > len(md):
                raise MetadataError("truncated stream headers")
            off, size = struct.unpack_from("<II", md, pos)
            pos += 8
            end = md.index(b"\0", pos)
            name = md[pos:end].decode("ascii", "replace")
            pos = (end + 1 + 3) & ~3
            if off + size > len(md):
                raise MetadataError(f"stream {name} lies outside metadata")
            self.streams[name] = (off, size)
        tn = self.streams.get("#~") or self.streams.get("#-")
        if tn is None:
            raise MetadataError("no #~ table stream")
        self.strings = self.streams.get("#Strings", (0, 0))
        self.blob = self.streams.get("#Blob", (0, 0))
        t0, tsize = tn
        heap = md[t0 + 6]
        self.str_w = 4 if heap & 1 else 2
        self.guid_w = 4 if heap & 2 else 2
        self.blob_w = 4 if heap & 4 else 2
        valid = struct.unpack_from("<Q", md, t0 + 8)[0]
        p = t0 + 24
        self.rows: dict[int, int] = {}
        for t in range(64):
            if valid >> t & 1:
                self.rows[t] = struct.unpack_from("<I", md, p)[0]
                p += 4
        self.offsets: dict[int, int] = {}
        self.row_size: dict[int, int] = {}
        self.layout: dict[int, list[int]] = {}
        for t in range(0x2D):
            n = self.rows.get(t, 0)
            if n == 0:
                continue
            widths = [self._width(c) for c in _SCHEMA[t]]
            self.layout[t] = widths
            self.row_size[t] = sum(widths)
            self.offsets[t] = p
            p += n * self.row_size[t]
            if p > t0 + tsize + 8 and p > len(md):
                raise MetadataError("table data runs past metadata")

    def _width(self, col: Any) -> int:
        if isinstance(col, int):
            return col
        if col == _S:
            return self.str_w
        if col == _G:
            return self.guid_w
        if col == _B:
            return self.blob_w
        kind, ref = col
        if kind == "t":
            return 4 if self.rows.get(ref, 0) >= 65536 else 2
        bits, tables = _CODED[ref]
        mx = max((self.rows.get(t, 0) for t in tables if t is not None), default=0)
        return 4 if mx >= (1 << (16 - bits)) else 2

    def row(self, table: int, idx: int) -> list[int]:
        n = self.rows.get(table, 0)
        if idx < 1 or idx > n:
            raise MetadataError(f"row {idx} out of range for table 0x{table:02x}")
        p = self.offsets[table] + (idx - 1) * self.row_size[table]
        out = []
        for w in self.layout[table]:
            out.append(int.from_bytes(self.md[p:p + w], "little"))
            p += w
        return out

    def string(self, off: int) -> str:
        base, size = self.strings
        if off >= size:
            return ""
        end = self.md.find(b"\0", base + off, base + size)
        end = base + size if end < 0 else end
        return self.md[base + off:end].decode("utf-8", "replace")

    def blob_at(self, off: int) -> bytes:
        base, size = self.blob
        if off == 0 or off >= size:
            return b""
        b0 = self.md[base + off]
        if b0 & 0x80 == 0:
            n, hdr = b0, 1
        elif b0 & 0xC0 == 0x80:
            n, hdr = ((b0 & 0x3F) << 8) | self.md[base + off + 1], 2
        else:
            n, hdr = struct.unpack(">I", self.md[base + off:base + off + 4])[0] & 0x1FFFFFFF, 4
        return self.md[base + off + hdr: base + off + hdr + n]

    def coded(self, kind: str, value: int) -> tuple[int | None, int]:
        bits, tables = _CODED[kind]
        tag = value & ((1 << bits) - 1)
        return (tables[tag] if tag < len(tables) else None), value >> bits


def _token(public_key: bytes, full: bool) -> str:
    if not public_key:
        return ""
    if full:
        return hashlib.sha1(public_key, usedforsecurity=False).digest()[-8:][::-1].hex()
    return public_key.hex()


def _framework_hint(refs: list[dict[str, Any]]) -> str | None:
    names = {r["name"] for r in refs}
    if "System.Private.CoreLib" in names or "System.Runtime" in names:
        return "netcore-style references (System.Runtime)"
    if "netstandard" in names:
        return "netstandard references"
    if "mscorlib" in names:
        return "mscorlib references (.NET Framework / Mono profile)"
    return None


def read_clr_metadata(path: Path | str, *, max_types: int = 200_000) -> dict[str, Any]:
    """Parse a managed PE. Raises MetadataError for non-managed or malformed input."""
    import pefile
    p = Path(path)
    try:
        pe = pefile.PE(str(p), fast_load=True)
    except (pefile.PEFormatError, OSError) as e:
        raise MetadataError(f"not a PE file: {e}") from e
    try:
        dd = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14] if len(pe.OPTIONAL_HEADER.DATA_DIRECTORY) > 14 else None
        if dd is None or dd.VirtualAddress == 0 or dd.Size < 72:
            raise MetadataError("PE has no CLR header (native code, not a .NET assembly)")
        try:
            clr = pe.get_data(dd.VirtualAddress, 72)
            cb, _maj, _min, md_rva, md_size, cor_flags, ep_token = struct.unpack_from("<IHHIIII", clr, 0)
            if md_rva == 0 or md_size == 0 or md_size > 512 * 1024 * 1024:
                raise MetadataError("CLR header points at no/oversized metadata")
            md = pe.get_data(md_rva, md_size)
        except (pefile.PEFormatError, struct.error) as e:
            raise MetadataError(f"cannot read CLR metadata: {e}") from e
        machine = pefile.MACHINE_TYPE.get(pe.FILE_HEADER.Machine, hex(pe.FILE_HEADER.Machine))
        is_dll = bool(pe.FILE_HEADER.Characteristics & 0x2000)
        magic = pe.OPTIONAL_HEADER.Magic
    finally:
        pe.close()
    try:
        t = _Tables(md)
        asm: dict[str, Any] | None = None
        if t.rows.get(0x20):
            r = t.row(0x20, 1)
            pk = t.blob_at(r[6])
            asm = {"name": t.string(r[7]), "version": ".".join(str(x) for x in r[1:5]), "culture": t.string(r[8]) or "neutral",
                   "public_key_token": _token(pk, True), "flags": r[5]}
        refs = []
        for i in range(1, t.rows.get(0x23, 0) + 1):
            r = t.row(0x23, i)
            refs.append({"name": t.string(r[6]), "version": ".".join(str(x) for x in r[0:4]),
                         "public_key_token": _token(t.blob_at(r[5]), bool(r[4] & 1))})
        module_name = t.string(t.row(0x00, 1)[1]) if t.rows.get(0x00) else ""
        # --- type table ---------------------------------------------------------------------------------------
        n_types = t.rows.get(0x02, 0)
        td = [t.row(0x02, i) for i in range(1, min(n_types, max_types) + 1)]
        nested_of: dict[int, int] = {}
        for i in range(1, t.rows.get(0x29, 0) + 1):
            r = t.row(0x29, i)
            nested_of[r[0]] = r[1]

        def base_name(extends: int) -> str:
            tab, row = t.coded("TypeDefOrRef", extends)
            if tab == 0x01 and row:
                rr = t.row(0x01, row)
                ns = t.string(rr[2])
                return f"{ns}.{t.string(rr[1])}" if ns else t.string(rr[1])
            return ""

        names: dict[int, str] = {}

        def full_name(i: int) -> str:
            if i in names:
                return names[i]
            row = td[i - 1]
            nm = t.string(row[1])
            if i in nested_of and nested_of[i] != i and 0 < nested_of[i] <= len(td):
                out = f"{full_name(nested_of[i])}.{nm}"
            else:
                ns = t.string(row[2])
                out = f"{ns}.{nm}" if ns else nm
            names[i] = out
            return out

        types = []
        for i, row in enumerate(td, 1):
            flags = row[0]
            if flags & 0x20:
                kind = "interface"
            else:
                b = base_name(row[3])
                kind = {"System.Enum": "enum", "System.ValueType": "struct", "System.MulticastDelegate": "delegate",
                        "System.Delegate": "delegate"}.get(b, "class")
            vis = flags & 7
            types.append({"name": full_name(i), "kind": kind, "nested": i in nested_of, "namespace": t.string(row[2]),
                          "public": vis in (1, 2), "token": f"0x{0x02000000 + i:08x}"})
        # --- target framework attribute ----------------------------------------------------------------------
        tfm = None
        for i in range(1, t.rows.get(0x0C, 0) + 1):
            r = t.row(0x0C, i)
            ptab, prow = t.coded("HasCustomAttribute", r[0])
            if ptab != 0x20 or prow != 1:
                continue
            ctab, crow = t.coded("CustomAttributeType", r[1])
            if ctab != 0x0A:
                continue
            mr = t.row(0x0A, crow)
            cls_tab, cls_row = t.coded("MemberRefParent", mr[0])
            if cls_tab != 0x01:
                continue
            tr = t.row(0x01, cls_row)
            if t.string(tr[1]) == "TargetFrameworkAttribute":
                blob = t.blob_at(r[2])
                if len(blob) > 3 and blob[:2] == b"\x01\x00":
                    n = blob[2]
                    start = 3
                    if n & 0x80:
                        n = ((n & 0x3F) << 8) | blob[3]
                        start = 4
                    tfm = blob[start:start + n].decode("utf-8", "replace")
                break
    except (struct.error, IndexError, ValueError) as e:
        raise MetadataError(f"malformed metadata tables: {e}") from e
    return {
        "is_managed": True, "runtime_version": t.version, "module_name": module_name, "assembly": asm,
        "target_framework": tfm, "framework_hint": _framework_hint(refs) if tfm is None else None,
        "references": refs, "type_count": n_types, "types": types, "method_count": t.rows.get(0x06, 0),
        "pe": {"machine": machine, "is_dll": is_dll, "pe32_plus": magic == 0x20B, "il_only": bool(cor_flags & 1),
               "requires_32bit": bool(cor_flags & 2), "strong_name_signed": bool(cor_flags & 8),
               "entry_point_token": f"0x{ep_token:08x}" if ep_token else None},
        "metadata_tables_present": sorted(f"0x{k:02x}" for k in t.rows),
    }


# =====================================================================================================================
# Unity detection
# =====================================================================================================================
_UNITY_ASM_RE = re.compile(r"^Assembly-(CSharp|UnityScript)(-firstpass|-Editor)?\.dll$", re.I)


def detect_unity(path: Path | str) -> dict[str, Any]:
    """Classify the Unity layout around a file or directory. Pure filesystem inspection, bounded."""
    p = Path(path)
    start = p if p.is_dir() else p.parent
    roots = [start] + list(start.parents)[:4]
    il2cpp_evidence: list[str] = []
    mono_evidence: list[str] = []
    for root in roots:
        try:
            entries = {e.name: e for e in os.scandir(root)}
        except OSError:
            continue
        has_ga = any(n.lower() in ("gameassembly.dll", "gameassembly.so", "libil2cpp.so", "gameassembly.dylib") for n in entries)
        meta = [e.path for n, e in entries.items() if n.lower().endswith("_data") and
                (Path(e.path) / "il2cpp_data" / "Metadata" / "global-metadata.dat").is_file()]
        if (root / "il2cpp_data" / "Metadata" / "global-metadata.dat").is_file():
            meta.append(str(root / "il2cpp_data"))
        if has_ga and meta:
            il2cpp_evidence += [str(root / n) for n in entries if n.lower().startswith(("gameassembly", "libil2cpp"))]
            il2cpp_evidence += [str(Path(m) / "il2cpp_data" / "Metadata" / "global-metadata.dat") if not m.endswith("il2cpp_data")
                                else str(Path(m) / "Metadata" / "global-metadata.dat") for m in meta]
        for n, e in entries.items():
            if n.lower().endswith("_data") and (Path(e.path) / "Managed" / "Assembly-CSharp.dll").is_file():
                mono_evidence.append(str(Path(e.path) / "Managed" / "Assembly-CSharp.dll"))
        if (root / "Managed" / "Assembly-CSharp.dll").is_file():
            mono_evidence.append(str(root / "Managed" / "Assembly-CSharp.dll"))
        if il2cpp_evidence:
            break
    if p.is_file() and _UNITY_ASM_RE.match(p.name):
        mono_evidence.append(str(p))
    if il2cpp_evidence:
        return {"profile": "unity_il2cpp", "supported": False, "experimental": True, "evidence": sorted(set(il2cpp_evidence)),
                "reason": ("IL2CPP builds compile C# to native code (GameAssembly) with a global-metadata.dat; "
                           "there are no real IL method bodies for ILSpy to decompile. Not supported by this backend "
                           "and no recovery is attempted or implied."),
                "next_action": "Use a dedicated IL2CPP metadata dumper outside this tool (not bundled); native analysis of GameAssembly goes through the Rizin path."}
    if mono_evidence:
        return {"profile": "unity_mono", "supported": True, "experimental": False, "evidence": sorted(set(mono_evidence)),
                "reason": "Mono scripting backend: Assembly-CSharp*.dll holds real IL.", "next_action": ""}
    return {"profile": "dotnet", "supported": True, "experimental": False, "evidence": [], "reason": "", "next_action": ""}


# =====================================================================================================================
# decompile output scanning
# =====================================================================================================================
def count_markers(text: str) -> dict[str, int]:
    il = len(_IL_ERROR_RE.findall(text))
    cm = len(_COMMENT_ERROR_RE.findall(text))
    unk = len(_UNKNOWN_RESULT_RE.findall(text))
    return {"il_error_markers": il, "comment_error_markers": cm, "error_markers": il + cm, "unknown_result_warnings": unk}


def _expected_type_path(name: str, namespace: str) -> str:
    leaf = name[len(namespace) + 1:] if namespace and name.startswith(namespace + ".") else name
    return f"{namespace}/{leaf}.cs" if namespace else f"{leaf}.cs"


def _dotnet_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    env.setdefault("DOTNET_NOLOGO", "1")
    env.setdefault("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1")
    if "DOTNET_ROOT" not in env:
        dn = shutil.which("dotnet")
        if dn:
            root = Path(dn).resolve().parent
            if (root / "shared").is_dir():
                env["DOTNET_ROOT"] = str(root)
    return env


_PROBE_CACHE: dict[tuple[str, float], tuple[str | None, str]] = {}


class ILSpyBackend(BackendAdapter):
    backend_id = BACKEND_ID

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # -- discovery -----------------------------------------------------------------------------------------------
    def find_tool(self) -> Path | None:
        return discover_executable(self.settings, [TOOL_NAME], subdirs=["ilspy", "ilspycmd", "ilspycmd/tools"],
                                   extra_dirs=[Path.home() / ".dotnet" / "tools"])

    def _tool_dll_sha(self, exe: Path) -> str:
        """sha256 of the installed ilspycmd.dll (the nupkg digest is not recorded locally)."""
        store = exe.parent / ".store" / TOOL_NAME
        try:
            for dll in store.glob("*/ilspycmd/*/tools/*/any/ilspycmd.dll"):
                return sha256_of(dll)
        except OSError:
            pass
        return ""

    def probe(self) -> BackendInfo:
        exe = self.find_tool()
        if exe is None:
            tool: ToolProbe = probe_error(TOOL_NAME, "ilspycmd not found in tools dir, PATH or ~/.dotnet/tools", INSTALL_HINT,
                                          prerequisites=list(PREREQUISITES), license=LICENSE, source=SOURCE_URL, pinned=PINNED_VERSION)
        else:
            tool = self._probe_exe(exe)
        nxt = getattr(tool, "next_action", "")
        return BackendInfo(
            backend_id=BACKEND_ID, title="ILSpy (.NET / Unity Mono decompiler)",
            formats=["dotnet", "unity_mono"], platforms=["linux", "windows", "macos"],
            profiles=["dotnet", "unity_mono"],
            operations=[
                Operation("metadata", "Assembly identity, references, target framework and type table", {"module_path": "path"}, {"metadata": "dict"}),
                Operation("decompile", "Decompile to a C# project with a per-type recovery report", {"module_path": "path", "out_dir": "path"}, {"recovery_report": "dict"}),
                Operation("detect", "Classify .NET vs Unity Mono vs Unity IL2CPP (unsupported)", {"path": "path"}, {"profile": "dict"}),
            ],
            tools=[tool], resources={"typical_seconds_per_assembly": "1-30", "ram_mb": 300, "next_action": nxt,
                                     "il2cpp": "unsupported (reported, never faked)"},
            experimental=False)

    def _probe_exe(self, exe: Path) -> ToolProbe:
        try:
            key = (str(exe), exe.stat().st_mtime)
        except OSError:
            key = (str(exe), 0.0)
        if key in _PROBE_CACHE:
            ver, detail = _PROBE_CACHE[key]
        else:
            ver, detail = None, ""
            try:
                r = run_bounded(None, [str(exe), "--version"], limits=self.settings.limits, env=_dotnet_env(), timeout=30)
                first = r.text.strip().splitlines()[0] if r.text.strip() else ""
                if r.returncode == 0 and first.lower().startswith("ilspycmd"):
                    ver = first.split(":", 1)[1].strip()
                else:
                    detail = (r.err_text.strip() or r.text.strip() or f"exit code {r.returncode}")[:500]
            except (StageError, OSError) as e:
                detail = f"{type(e).__name__}: {e}"
            _PROBE_CACHE[key] = (ver, detail)
        if ver is None:
            dn = shutil.which("dotnet")
            nxt = ("Install the .NET 8 runtime (https://dotnet.microsoft.com/download/dotnet/8.0) and make `dotnet` resolvable"
                   if dn is None else "ilspycmd is present but did not run; check the .NET 8 runtime and DOTNET_ROOT")
            return RecoveryToolProbe(TOOL_NAME, Availability.DETECTED, path=str(exe), detail=f"found but --version failed: {detail}",
                                     prerequisites=list(PREREQUISITES), license=LICENSE, source=SOURCE_URL,
                                     pinned=PINNED_VERSION, next_action=nxt)
        note = "" if ver == PINNED_VERSION else f"version differs from pinned {PINNED_VERSION}"
        return RecoveryToolProbe(TOOL_NAME, Availability.INSTALLED, path=str(exe), version=ver, detail=note,
                                 prerequisites=list(PREREQUISITES), license=LICENSE, source=SOURCE_URL, pinned=PINNED_VERSION,
                                 integrity=self._tool_dll_sha(exe),
                                 next_action="" if not note else f"install the pinned version: {INSTALL_HINT}")

    def smoke(self) -> ToolProbe:
        tool = self.probe().tools[0]
        if tool.availability not in (Availability.INSTALLED, Availability.USABLE) or not tool.path:
            return tool
        with tempfile.TemporaryDirectory(prefix="rs-ilspy-smoke-") as td:
            dll = Path(td) / "SmokeProbe.dll"
            dll.write_bytes(base64.b64decode("".join(_SMOKE_DLL_B64.split())))
            try:
                r = run_bounded(None, [tool.path, "--disable-updatecheck", str(dll)], limits=self.settings.limits,
                                env=_dotnet_env(), timeout=120)
            except (StageError, OSError) as e:
                tool.detail = f"smoke failed: {e}"
                return tool
            out = r.text
            if r.returncode == 0 and "public static int Answer()" in out and "return 42;" in out:
                tool.availability = Availability.USABLE
                tool.detail = "decompiled the built-in sample assembly (Rebuild.Smoke.Probe.Answer)"
            else:
                tool.detail = f"smoke failed: exit {r.returncode}: {(r.err_text or out)[:300]}"
        return tool

    # -- operations ----------------------------------------------------------------------------------------------
    def _tool_version(self) -> tuple[Path | None, str | None]:
        exe = self.find_tool()
        if exe is None:
            return None, None
        t = self._probe_exe(exe)
        return exe, t.version

    def detect(self, path: Path | str, **_: Any) -> OperationResult:
        return OperationResult(ok=True, data={"profile": detect_unity(path)})

    def op_detect(self, ctx: Any, path: str, **kw: Any) -> OperationResult:
        return self.detect(path)

    def op_metadata(self, ctx: Any, module_path: str, **kw: Any) -> OperationResult:
        return self.metadata(module_path, ctx=ctx, **kw)

    def op_decompile(self, ctx: Any, module_path: str, out_dir: str, **kw: Any) -> OperationResult:
        return self.decompile(module_path, out_dir, ctx=ctx, **kw)

    def metadata(self, module_path: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
                 module_id: str | None = None, timeout: float = 120) -> OperationResult:
        mp = Path(module_path)
        if not mp.is_file():
            return OperationResult(ok=False, error=f"module not found: {mp}")
        try:
            md = read_clr_metadata(mp)
        except MetadataError as e:
            return OperationResult(ok=False, error=str(e), data={"profile": detect_unity(mp), "is_managed": False})
        sha = sha256_file(mp)
        exe, ver = self._tool_version()
        prof = detect_unity(mp)
        refs = md["references"]
        if prof["profile"] == "dotnet" and any(r["name"].startswith("UnityEngine") for r in refs):
            prof = {**detect_unity(mp), "profile": "unity_mono", "evidence": ["references UnityEngine"]}
        listing: dict[str, Any] = {"source": "native_metadata", "tool_listing_available": False}
        warnings: list[str] = []
        tool_types: list[str] | None = None
        if exe is not None and ver:
            try:
                r = run_bounded(ctx, [str(exe), "--disable-updatecheck", "-l", "cisde", str(mp)], limits=self.settings.limits,
                                env=_dotnet_env(), timeout=timeout)
                if r.returncode == 0:
                    tool_types = [ln.split(" ", 1)[1].strip() for ln in r.text.splitlines() if " " in ln and ln.split(" ", 1)[0] in
                                  ("Class", "Interface", "Struct", "Delegate", "Enum")]
                    native = sorted(x["name"] for x in md["types"])
                    listing = {"source": "ilspycmd -l cisde", "tool_listing_available": True, "tool_type_count": len(tool_types),
                               "matches_native_metadata": sorted(tool_types) == native}
                    if sorted(tool_types) != native:
                        warnings.append("ilspycmd type listing differs from native metadata type table")
                else:
                    warnings.append(f"ilspycmd -l exited {r.returncode}: {r.err_text[:300]}")
            except StageError as e:
                warnings.append(str(e))
        else:
            warnings.append("ilspycmd not available; metadata comes from the native reader only. " + INSTALL_HINT)
        user_types = [x for x in md["types"] if x["name"] != "<Module>"]
        body = {
            "schema": SCHEMA_VERSION, "module": {"path": str(mp), "sha256": sha, "size": mp.stat().st_size},
            "assembly": md["assembly"], "target_framework": md["target_framework"], "framework_hint": md["framework_hint"],
            "runtime_version": md["runtime_version"], "pe": md["pe"], "profile": prof,
            "references": md["references"], "reference_count": len(md["references"]),
            "type_count": md["type_count"], "user_type_count": len(user_types),
            "top_level_type_count": sum(1 for x in user_types if not x["nested"]), "method_count": md["method_count"],
            "types": cap_list(user_types, LIST_CAP), "type_listing_check": listing, "warnings": warnings,
            "tool": {"name": TOOL_NAME, "version": ver},
        }
        inputs = {"op": "metadata", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": TOOL_NAME, "tool_version": ver,
                  "module_sha256": sha}
        eids: list[str] = []
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "dotnet.metadata", f"Assembly metadata: {mp.name}", body,
                              module_id=module_id, inputs=inputs, producer=BACKEND_ID)
        if eid:
            eids.append(eid)
        return OperationResult(ok=True, data=body, evidence_ids=eids, truncated=body["types"]["truncated"])

    def decompile(self, module_path: Path | str, out_dir: Path | str, *, ctx: Any = None, studio: Any = None,
                  case_id: str | None = None, module_id: str | None = None, source_root: Path | str | None = None,
                  timeout: float = 600, language_version: str | None = None, reference_dir: Path | str | None = None,
                  fallback_max_types: int = FALLBACK_MAX_TYPES, fallback_budget_s: float = FALLBACK_BUDGET_S) -> OperationResult:
        mp, out = Path(module_path), Path(out_dir)
        if not mp.is_file():
            return OperationResult(ok=False, error=f"module not found: {mp}")
        # --- path policy ---------------------------------------------------------------------------------------
        try:
            if source_root is not None:
                assert_output_not_in_source(out, Path(source_root))
            if is_within(resolve_final(mp), resolve_final(out)):
                raise PathPolicyError(f"output directory {out} contains the module being decompiled")
        except PathPolicyError as e:
            return OperationResult(ok=False, error=f"path policy: {e}")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            return OperationResult(ok=False, error=f"output directory {out} exists and is not empty; refusing to mix outputs")
        prof = detect_unity(mp)
        sha = sha256_file(mp)
        exe, ver = self._tool_version()
        studio_obj = resolve_studio(ctx, studio)
        inputs_base = {"op": "decompile", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": TOOL_NAME, "tool_version": ver,
                       "module_sha256": sha, "language_version": language_version}
        # --- IL2CPP / native modules: report, never fake -------------------------------------------------------
        try:
            md = read_clr_metadata(mp)
        except MetadataError as e:
            body = {"schema": SCHEMA_VERSION, "status": "unsupported", "module": {"path": str(mp), "sha256": sha},
                    "profile": prof["profile"], "profile_detail": prof, "reason": str(e),
                    "recovered": False, "equivalence_claimed": False}
            eid = record_evidence(studio_obj, case_id, "dotnet.profile", f"Not decompilable: {mp.name}", body, module_id=module_id,
                                  inputs=inputs_base, producer=BACKEND_ID)
            return OperationResult(ok=False, error=f"{mp.name} is not a managed assembly: {e}"
                                   + (f" ({prof['reason']})" if prof["profile"] == "unity_il2cpp" else ""),
                                   data=body, evidence_ids=[eid] if eid else [])
        if prof["profile"] == "unity_il2cpp":
            body = {"schema": SCHEMA_VERSION, "status": "unsupported", "module": {"path": str(mp), "sha256": sha},
                    "profile": "unity_il2cpp", "experimental": True, "profile_detail": prof,
                    "reason": "managed assemblies inside an IL2CPP layout are metadata stubs without method bodies",
                    "recovered": False, "equivalence_claimed": False}
            eid = record_evidence(studio_obj, case_id, "dotnet.profile", f"IL2CPP unsupported: {mp.name}", body, module_id=module_id,
                                  inputs=inputs_base, producer=BACKEND_ID)
            return OperationResult(ok=False, error="unity_il2cpp is experimental/unsupported here: no recovery attempted. " + prof["reason"],
                                   data=body, evidence_ids=[eid] if eid else [])
        if any(r["name"].startswith("UnityEngine") for r in md["references"]) and prof["profile"] == "dotnet":
            prof = {**prof, "profile": "unity_mono", "evidence": ["references UnityEngine"]}
        if exe is None or ver is None:
            return OperationResult(ok=False, error=f"ilspycmd is not usable on this host. next_action: {INSTALL_HINT}",
                                   data={"next_action": INSTALL_HINT})
        ref_args: list[str] = []
        if reference_dir:
            ref_args = ["-r", str(reference_dir)]
        lang_args = ["-lv", language_version] if language_version else []
        out.mkdir(parents=True, exist_ok=True)
        cmd = [str(exe), "--disable-updatecheck", *lang_args, *ref_args, "-p", "-o", str(out), str(mp)]
        try:
            r = run_bounded(ctx, cmd, limits=self.settings.limits, env=_dotnet_env(), timeout=timeout)
        except StageError as e:
            return OperationResult(ok=False, error=str(e))
        mode = "project"
        failures: list[dict[str, str]] = []
        project_stderr = ""
        top_types = [t for t in md["types"] if t["name"] != "<Module>" and not t["nested"]]
        status_by_type: dict[str, dict[str, Any]] = {}
        if r.returncode != 0:
            project_stderr = (r.err_text or r.text)[:4000]
            seen_members: set[str] = set()
            for m in _DECOMPILE_FAILURE_RE.finditer(r.err_text + r.text):
                if m.group(2) not in seen_members and len(seen_members) < 100:
                    seen_members.add(m.group(2))
                    failures.append({"type": m.group(2).rsplit(".", 1)[0], "member": m.group(2), "token": "0x" + m.group(1),
                                     "message": "whole-project run aborted: method body could not be read"})
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True, exist_ok=True)
            mode = "per_type_fallback"
            deadline = time.time() + fallback_budget_s
            attempted = 0
            for t in top_types:
                if attempted >= fallback_max_types or time.time() > deadline:
                    status_by_type[t["name"]] = {"status": "not_attempted", "file": None, "error_markers": 0}
                    continue
                attempted += 1
                try:
                    tr = run_bounded(ctx, [str(exe), "--disable-updatecheck", *lang_args, *ref_args, "-t", t["name"], str(mp)],
                                     limits=self.settings.limits, env=_dotnet_env(), timeout=min(timeout, 300))
                except StageError as e:
                    status_by_type[t["name"]] = {"status": "failed", "file": None, "error_markers": 0, "message": str(e)[:300]}
                    continue
                rel = _expected_type_path(t["name"], t["namespace"])
                if tr.returncode != 0 or not tr.text.strip():
                    msg = (tr.err_text or "no output")
                    mm = _DECOMPILE_FAILURE_RE.search(msg)
                    status_by_type[t["name"]] = {"status": "failed", "file": None, "error_markers": 0,
                                                 "message": (mm.group(0) if mm else msg.splitlines()[0] if msg.strip() else "no output")[:300]}
                    continue
                dest = out / "types" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(tr.text, encoding="utf-8")
                mk = count_markers(tr.text)
                status_by_type[t["name"]] = {"status": "decompiled_with_errors" if mk["error_markers"] else "decompiled",
                                             "file": f"types/{rel}", "error_markers": mk["error_markers"]}
        # --- scan output -----------------------------------------------------------------------------------------
        files: list[dict[str, Any]] = []
        totals = {"il_error_markers": 0, "comment_error_markers": 0, "error_markers": 0, "unknown_result_warnings": 0}
        digest = hashlib.sha256()
        scanned = 0
        file_markers: dict[str, int] = {}
        for fp in sorted(out.rglob("*")):
            if not fp.is_file() or fp.is_symlink():
                continue
            scanned += 1
            if scanned > self.settings.limits.max_inventory_files:
                break
            rel = fp.relative_to(out).as_posix()
            data = fp.read_bytes()
            h = hashlib.sha256(data).hexdigest()
            digest.update(f"{rel}\0{h}\n".encode())
            entry: dict[str, Any] = {"path": rel, "bytes": len(data), "sha256": h}
            if fp.suffix == ".cs":
                text = data.decode("utf-8", "replace")
                mk = count_markers(text)
                entry["lines"] = text.count("\n") + 1
                entry.update(mk)
                for k in totals:
                    totals[k] += mk[k]
                file_markers[rel] = mk["error_markers"]
            files.append(entry)
        if mode == "project":
            for t in top_types:
                rel = _expected_type_path(t["name"], t["namespace"])
                if rel in file_markers:
                    n = file_markers[rel]
                    status_by_type[t["name"]] = {"status": "decompiled_with_errors" if n else "decompiled", "file": rel, "error_markers": n}
                else:
                    status_by_type[t["name"]] = {"status": "unmapped", "file": None, "error_markers": 0}
        counts = {s: sum(1 for v in status_by_type.values() if v["status"] == s)
                  for s in ("decompiled", "decompiled_with_errors", "failed", "not_attempted", "unmapped")}
        # references that are not next to the module (a common cause of weaker output)
        unresolved = sorted({r["name"] for r in md["references"]
                             if not (mp.parent / f"{r['name']}.dll").exists() and not r["name"].startswith(("System", "Microsoft", "netstandard", "mscorlib", "WindowsBase"))})
        type_listing = [{"name": t["name"], "kind": t["kind"], **status_by_type.get(t["name"], {"status": "unknown", "file": None, "error_markers": 0}),
                        **({"message": status_by_type[t["name"]]["message"]} if "message" in status_by_type.get(t["name"], {}) else {})}
                        for t in top_types]
        project_files = [f["path"] for f in files if f["path"].endswith(".csproj")]
        report = {
            "schema": SCHEMA_VERSION, "status": "ok" if counts["failed"] == 0 and counts["not_attempted"] == 0 else "partial",
            "profile": prof["profile"], "profile_detail": prof, "mode": mode, "project_file": project_files[0] if project_files else None,
            "module": {"path": str(mp), "sha256": sha, "assembly": md["assembly"], "target_framework": md["target_framework"]},
            "tool": {"name": TOOL_NAME, "version": ver, "command": "ilspycmd --disable-updatecheck -p -o <out> <module>"},
            "types_total": len(top_types), "types_decompiled": counts["decompiled"], "types_with_errors": counts["decompiled_with_errors"],
            "types_failed": counts["failed"], "types_not_attempted": counts["not_attempted"], "types_unmapped": counts["unmapped"],
            **totals, "failures": failures[:200], "project_run_stderr": project_stderr,
            "unresolved_references": unresolved[:200], "unresolved_reference_count": len(unresolved),
            "files": cap_list(files, LIST_CAP), "type_listing": cap_list(type_listing, LIST_CAP),
            "output_tree_sha256": digest.hexdigest(), "output_file_count": len(files),
            "claims": "Per-type decompilation status only. The C# is a reconstruction; it is not asserted to compile or to be equivalent to the original source.",
            "equivalence_claimed": False,
        }
        eid = record_evidence(studio_obj, case_id, "dotnet.recovery_report", f"ILSpy recovery report: {mp.name}", report,
                              module_id=module_id, inputs={**inputs_base, "mode_requested": "project", "reference_dir": str(reference_dir) if reference_dir else None},
                              producer=BACKEND_ID)
        truncated = report["files"]["truncated"] or report["type_listing"]["truncated"] or counts["not_attempted"] > 0
        ok = counts["failed"] == 0 or counts["decompiled"] + counts["decompiled_with_errors"] > 0
        return OperationResult(ok=ok, data={"recovery_report": report, "out_dir": str(out)}, evidence_ids=[eid] if eid else [],
                               truncated=truncated, error=None if ok else "no type could be decompiled")
