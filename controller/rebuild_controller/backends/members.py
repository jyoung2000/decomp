"""Native code that lives INSIDE another file: the slices of a fat/universal Mach-O and the native libraries of an Android package.

R7 routes each such member to the ordinary native analysis (rizin), one module per slice / per ABI library:

* fat Mach-O  -> one member per slice, ``slice/<arch>`` (byte range given by the fat header);
* APK / AAB   -> one member per ``lib/<abi>/*.so`` (``base/lib/<abi>/*.so`` in an .aab).

A member module carries ``meta.container = {"rel_path", "member", "kind", ...}`` and its own sha256. The bytes are copied ONCE into the
case work folder (``<case>/re/<module_id>/member/<name>``, never next to the user's files), re-checked against the recorded sha256
on every use, and analysed there. Nothing is executed. Everything is bounded: member count, member size, zip compression ratio.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import zipfile
from pathlib import Path
from typing import Any

from . import support as sp

MAX_MEMBERS = 64                       # per container
MAX_MEMBER_BYTES = 512 * 1024 * 1024   # a single slice / library
MAX_RATIO = 200                        # zip: uncompressed / compressed (zip-bomb guard)
ANDROID_ABIS = ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86", "riscv64", "mips", "mips64")
_LIB_RE = re.compile(r"^(?:base/)?lib/([^/]+)/([^/]+\.so)$")
MEMBER_SEP = "!/"


class MemberError(ValueError):
    pass


# --------------------------------------------------------------------------------------------- listing
def fat_slices(path: Path | str) -> list[dict[str, Any]]:
    """Slices of a fat Mach-O as members (arch, offset, size). Raises MemberError when the file is not fat."""
    info = sp.inspect_macho(path)
    if info["layout"] != "fat":
        raise MemberError("not a fat/universal Mach-O")
    size = Path(path).stat().st_size
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for s in info["slices"][:MAX_MEMBERS]:
        arch = s.get("arch") or s.get("declared_arch") or "unknown"
        if s.get("parse_note") or s["offset"] + s["size"] > size:
            out.append({"member": f"slice/{arch}", "kind": "fat_slice", "arch": arch, "offset": s["offset"], "size": s["size"],
                        "skip": s.get("parse_note") or "slice extends past the end of the file"})
            continue
        n = seen[arch] = seen.get(arch, 0) + 1
        name = arch if n == 1 else f"{arch}.{n}"
        out.append({"member": f"slice/{name}", "kind": "fat_slice", "arch": arch, "offset": s["offset"], "size": s["size"],
                    "encrypted": bool(s.get("encrypted"))})
    return out


def apk_native_libs(path: Path | str) -> list[dict[str, Any]]:
    """``lib/<abi>/*.so`` entries of an APK (``base/lib/...`` of an AAB) as members, with zip-bomb and size guards."""
    out: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as zf:
        for zi in zf.infolist():
            m = _LIB_RE.match(zi.filename)
            if not m or zi.is_dir():
                continue
            if len(out) >= MAX_MEMBERS:
                out.append({"member": zi.filename, "kind": "apk_lib", "abi": m.group(1), "skip": f"more than {MAX_MEMBERS} native libraries"})
                break
            rec = {"member": zi.filename, "kind": "apk_lib", "abi": m.group(1), "name": m.group(2), "size": zi.file_size,
                   "compressed": zi.compress_type != zipfile.ZIP_STORED}
            if zi.file_size > MAX_MEMBER_BYTES:
                rec["skip"] = f"larger than {MAX_MEMBER_BYTES} bytes"
            elif zi.compress_size and zi.file_size / max(1, zi.compress_size) > MAX_RATIO:
                rec["skip"] = "compression ratio is implausible (possible zip bomb)"
            elif m.group(1) not in ANDROID_ABIS:
                rec["note"] = f"unusual ABI folder {m.group(1)!r}"
            out.append(rec)
    return out


def list_members(path: Path | str, detection: dict[str, Any]) -> list[dict[str, Any]]:
    """Members worth analysing for a detected file (empty for anything that is not a fat Mach-O or an Android package)."""
    fmt, flags = detection.get("format"), detection.get("flags") or {}
    try:
        if fmt == "macho" and flags.get("layout") == "fat":
            return fat_slices(path)
        if fmt in ("apk", "aab"):
            return apk_native_libs(path)
    except (OSError, ValueError, zipfile.BadZipFile, struct.error) as e:
        return [{"member": "", "kind": "error", "skip": f"{type(e).__name__}: {e}"[:200]}]
    return []


# --------------------------------------------------------------------------------------------- bytes
def read_member(container: Path | str, spec: dict[str, Any]) -> bytes:
    """The member's bytes (bounded). ``spec`` is a list_members() record or a module's ``meta.container``."""
    kind = spec.get("kind")
    if kind == "fat_slice":
        off, size = int(spec["offset"]), int(spec["size"])
        if size <= 0 or size > MAX_MEMBER_BYTES:
            raise MemberError(f"slice size {size} out of bounds")
        with open(container, "rb") as f:
            f.seek(off)
            data = f.read(size)
        if len(data) != size:
            raise MemberError("slice extends past the end of the file")
        if sp.macho_kind(data[:8]) != "thin":
            raise MemberError("slice is not a thin Mach-O")
        return data
    if kind == "apk_lib":
        with zipfile.ZipFile(container) as zf:
            zi = zf.getinfo(spec["member"])
            if zi.file_size > MAX_MEMBER_BYTES or (zi.compress_size and zi.file_size / max(1, zi.compress_size) > MAX_RATIO):
                raise MemberError(f"{spec['member']}: refused (size {zi.file_size}, compressed {zi.compress_size})")
            with zf.open(zi) as fh:
                data = fh.read(MAX_MEMBER_BYTES + 1)
        if len(data) > MAX_MEMBER_BYTES:
            raise MemberError(f"{spec['member']}: larger than {MAX_MEMBER_BYTES} bytes")
        return data
    raise MemberError(f"unknown member kind {kind!r}")


def member_rel_path(container_rel: str, member: str) -> str:
    return f"{container_rel}{MEMBER_SEP}{member}"


def describe(container: Path | str, spec: dict[str, Any]) -> dict[str, Any]:
    """Read one member and detect what it is: {sha256, size, detect} (detect = Detection.to_dict() of the member's bytes)."""
    from .detect import sniff_bytes
    data = read_member(container, spec)
    name = spec.get("name") or (Path(str(container)).name if spec.get("kind") == "fat_slice" else spec["member"].rsplit("/", 1)[-1])
    det = sniff_bytes(name, data)
    if spec.get("kind") == "fat_slice":
        det.flags["slice_of_fat"] = True
    d = det.to_dict()
    if spec.get("abi"):
        d["flags"] = {**d.get("flags", {}), "android_abi": spec["abi"]}
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "detect": d}


def materialize(container: Path | str, spec: dict[str, Any], dest_dir: Path | str, *, sha256: str | None = None) -> Path:
    """Copy a member into ``dest_dir`` (the case work folder) once; verify the sha256 when given. Returns the copy's path."""
    dest_dir = Path(dest_dir)
    base = spec.get("name") or spec["member"].replace("/", "_")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120] or "member.bin"
    out = dest_dir / safe
    if out.is_file() and sha256 and _sha_cached(out) == sha256:
        return out
    data = read_member(container, spec)
    got = hashlib.sha256(data).hexdigest()
    if sha256 and got != sha256:
        raise MemberError(f"{spec['member']} changed inside its container since inventory (sha256 {got[:12]} != {sha256[:12]})")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _sha_cached(p: Path) -> str:
    """sha256 of the work-folder copy, re-hashed only when its size or mtime changed (module_file runs on every operation)."""
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        if len(_SHA_CACHE) > 256:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = _sha(p)
    return _SHA_CACHE[key]


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
