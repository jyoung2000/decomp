"""Packer / entropy / overlay / section-anomaly report, and a consented UPX unpack into the case work folder.

``packer_report(path)`` never executes the input. It reads the PE (or ELF) headers with pefile/struct and returns a plain
dict: per-section entropy and flags, overlay size/entropy, anomalies (writable+executable sections, executable sections with
no file data, entry point outside the first code section, almost no imports, ...), named packer signatures (section names,
``UPX!`` magic) and a verdict ``packed`` (bool) with ``packer`` (``"UPX"`` or a family name or ``"unknown"``) and the reasons.

``unpack_upx(src, out_dir, upx)`` runs the pinned ``upx -d`` in a separate process on a COPY of the input inside ``out_dir``
(the case work folder) and never writes next to, or over, the original. The original's sha256 is checked before and after.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HIGH_ENTROPY = 7.2           # compressed/encrypted data; compiled code sits around 5.5-6.6
MIN_ENTROPY_BYTES = 512      # do not judge entropy on tiny sections
MAX_READ = 256 * 1024 * 1024
UPX_MAGIC = b"UPX!"

# Section names written by packers/protectors (lower-cased, NUL-stripped). Name alone is a signature, not proof.
SECTION_SIGNATURES: dict[str, str] = {
    "upx0": "UPX", "upx1": "UPX", "upx2": "UPX", "upx3": "UPX", ".upx0": "UPX", ".upx1": "UPX",
    ".aspack": "ASPack", ".adata": "ASPack",
    ".mpress1": "MPRESS", ".mpress2": "MPRESS",
    ".petite": "Petite", "pec2": "PECompact", "pecompact2": "PECompact", "pec2to": "PECompact",
    ".nsp0": "NsPack", ".nsp1": "NsPack", ".nsp2": "NsPack", "nsp0": "NsPack", "nsp1": "NsPack",
    ".themida": "Themida", ".winlice": "WinLicense",
    ".vmp0": "VMProtect", ".vmp1": "VMProtect", ".vmp2": "VMProtect",
    ".enigma1": "Enigma", ".enigma2": "Enigma",
    ".rlpack": "RLPack", ".packed": "RLPack", "pebundle": "PEBundle", ".perplex": "Perplex",
    ".yp": "Y0da", ".y0da": "Y0da", "mew": "MEW", ".ccg": "CCG", "kkrunchy": "kkrunchy",
}
UNPACKERS = {"UPX": "upx -d"}   # families with a pinned, consented unpacker


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def packer_report(path: Path | str) -> dict[str, Any]:
    """Static packer check of one file. Never raises for malformed input: errors are reported in the dict."""
    p = Path(path)
    rep: dict[str, Any] = {"schema": "rebuild-studio.packer-report/1", "file": p.name, "format": None, "packed": False,
                           "packer": None, "confidence": 0.0, "reasons": [], "anomalies": [], "sections": [],
                           "overlay": None, "signatures": [], "unpacker": None}
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            data = f.read(min(size, MAX_READ))
    except OSError as e:
        rep["error"] = f"{type(e).__name__}: {e}"
        return rep
    rep["size"] = size
    rep["file_entropy"] = round(entropy(data), 3)
    if data[:2] == b"MZ":
        _pe(data, rep)
    elif data[:4] == b"\x7fELF":
        _elf(data, rep)
    else:
        rep["format"] = "other"
    _verdict(rep)
    return rep


def _pe(data: bytes, rep: dict[str, Any]) -> None:
    rep["format"] = "pe"
    try:
        import pefile
        pe = pefile.PE(data=data, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR"]])
    except Exception as e:
        rep["error"] = f"pe parse: {type(e).__name__}: {str(e)[:200]}"
        rep["anomalies"].append("PE headers could not be parsed")
        return
    try:
        com = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14] if len(pe.OPTIONAL_HEADER.DATA_DIRECTORY) > 14 else None
        rep["dotnet"] = bool(com and com.VirtualAddress and com.Size)
        ep = pe.OPTIONAL_HEADER.AddressOfEntryPoint
        rep["entry_rva"] = f"0x{ep:x}"
        ep_section = None
        first_code = None
        end_of_sections = 0
        for i, s in enumerate(pe.sections):
            name = s.Name.rstrip(b"\0").decode("latin-1", "replace")
            raw = s.get_data()[: s.SizeOfRawData] if s.SizeOfRawData else b""
            ch = s.Characteristics
            x, w = bool(ch & 0x20000000), bool(ch & 0x80000000)
            ent = round(entropy(raw), 3) if raw else 0.0
            sec = {"name": name, "vaddr": f"0x{s.VirtualAddress:x}", "vsize": s.Misc_VirtualSize, "raw_size": s.SizeOfRawData,
                   "entropy": ent, "exec": x, "write": w}
            rep["sections"].append(sec)
            end_of_sections = max(end_of_sections, s.PointerToRawData + s.SizeOfRawData)
            if x and first_code is None:
                first_code = i
            if s.VirtualAddress <= ep < s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData):
                ep_section = i
            fam = SECTION_SIGNATURES.get(name.lower())
            if fam:
                rep["signatures"].append({"kind": "section_name", "family": fam, "detail": name})
            if x and w:
                rep["anomalies"].append(f"section {name!r} is writable and executable")
            if x and s.SizeOfRawData == 0 and s.Misc_VirtualSize > 0:
                rep["anomalies"].append(f"executable section {name!r} has no data in the file (filled at run time)")
            if x and len(raw) >= MIN_ENTROPY_BYTES and ent >= HIGH_ENTROPY:
                rep["anomalies"].append(f"executable section {name!r} has high entropy ({ent})")
            if s.Misc_VirtualSize and s.SizeOfRawData and s.Misc_VirtualSize > 8 * s.SizeOfRawData and s.Misc_VirtualSize > 0x10000:
                rep["anomalies"].append(f"section {name!r} is much larger in memory than in the file")
        if ep_section is None:
            rep["anomalies"].append("entry point is outside every section")
        else:
            rep["entry_section"] = rep["sections"][ep_section]["name"]
            if first_code is not None and ep_section != first_code:
                rep["anomalies"].append(f"entry point is in section {rep['entry_section']!r}, not the first code section")
        imports = 0
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
            imports += len(entry.imports or [])
        rep["import_count"] = imports
        if not rep["dotnet"] and imports < 8:
            rep["anomalies"].append(f"only {imports} imported functions")
        ov = pe.get_overlay_data_start_offset()
        if ov is None and end_of_sections and end_of_sections < len(data):
            ov = end_of_sections
        if ov is not None and ov < len(data):
            blob = data[ov:]
            rep["overlay"] = {"offset": ov, "size": len(blob), "entropy": round(entropy(blob[: 1 << 20]), 3)}
            if len(blob) > 4096 and rep["overlay"]["entropy"] >= HIGH_ENTROPY:
                rep["anomalies"].append(f"high-entropy overlay of {len(blob)} bytes after the last section")
        hdr_end = min(len(data), (pe.sections[0].PointerToRawData if pe.sections else 0x400) + 0x400)
        if UPX_MAGIC in data[:hdr_end]:
            rep["signatures"].append({"kind": "magic", "family": "UPX", "detail": "UPX! header"})
    finally:
        pe.close()


def _elf(data: bytes, rep: dict[str, Any]) -> None:
    rep["format"] = "elf"
    if UPX_MAGIC in data[:0x400] or UPX_MAGIC in data[-0x400:]:
        rep["signatures"].append({"kind": "magic", "family": "UPX", "detail": "UPX! header"})
    try:
        is64, le = data[4] == 2, data[5] == 1
        e = "<" if le else ">"
        shnum = struct.unpack_from(e + "H", data, 0x3C if is64 else 0x30)[0]
        if shnum == 0:
            rep["anomalies"].append("no section headers")
    except struct.error:
        rep["anomalies"].append("ELF header truncated")


def _verdict(rep: dict[str, Any]) -> None:
    fams = [s["family"] for s in rep["signatures"]]
    an = rep["anomalies"]
    strong = [a for a in an if "high entropy" in a or "no data in the file" in a or "high-entropy overlay" in a]
    if fams:
        fam = Counter(fams).most_common(1)[0][0]
        rep.update(packed=True, packer=fam, confidence=0.95 if (len(fams) > 1 or strong) else 0.8)
        rep["reasons"].append(f"{fam} signature: " + ", ".join(sorted({s['detail'] for s in rep['signatures']})))
    elif strong and len(an) >= 2:
        rep.update(packed=True, packer="unknown", confidence=0.6)
        rep["reasons"].append("no known packer signature, but: " + "; ".join(an[:4]))
    if rep["packed"]:
        rep["reasons"] += [a for a in an if a not in rep["reasons"]][:6]
        rep["unpacker"] = UNPACKERS.get(rep["packer"] or "")
    rep["summary"] = (f"packed ({rep['packer']}, confidence {rep['confidence']})" if rep["packed"]
                      else ("not packed" if not an else f"not packed; {len(an)} anomaly(ies) noted"))


# ------------------------------------------------------------------------------------------------ UPX unpack
@dataclass
class UpxTool:
    exe: Path
    version: str | None
    sha256: str | None
    pinned: bool


def _lock_upx() -> dict[str, Any] | None:
    for p in (Path(__file__).resolve().parents[1] / "data" / "dependency-lock.json",):
        try:
            return json.loads(p.read_text(encoding="utf-8"))["tools"]["upx"]
        except (OSError, KeyError, ValueError):
            continue
    return None


def find_upx(tools_dir: Path | str | None = None) -> UpxTool | None:
    """The pinned UPX from the tools folder (Tools page install) or REBUILD_STUDIO_TOOLS. A UPX on PATH is used only when
    its sha256 equals the pin (an unpinned UPX is never run)."""
    lock = _lock_upx() or {}
    want = (lock.get("layout") or {}).get("entry_sha256")
    if not want:
        return None
    cands: list[Path] = []
    if tools_dir:
        cands.append(Path(tools_dir) / "upx" / "upx.exe")
        cands.append(Path(tools_dir) / "upx" / "upx")
    if os.environ.get("REBUILD_STUDIO_TOOLS"):
        cands.append(Path(os.environ["REBUILD_STUDIO_TOOLS"]) / "upx" / ("upx.exe" if os.name == "nt" else "upx"))
    w = shutil.which("upx")
    if w:
        cands.append(Path(w))
    for c in cands:
        if not c.is_file():
            continue
        sha = sha256_path(c)
        if sha != want:
            continue
        ver = None
        try:
            r = subprocess.run([str(c), "--version"], capture_output=True, text=True, timeout=20)
            first = (r.stdout or "").splitlines()[:1]
            ver = first[0].split()[-1] if first else None
        except (OSError, subprocess.SubprocessError):
            continue
        return UpxTool(c, ver, sha, pinned=True)
    return None


class UnpackError(RuntimeError):
    pass


def unpack_upx(src: Path | str, out_dir: Path | str, upx: UpxTool, *, timeout: float = 120.0) -> dict[str, Any]:
    """Decompress a UPX-packed file into ``out_dir`` (case work folder). Never in place: the input is copied into
    ``out_dir/input/`` first, ``upx -d -o`` writes ``out_dir/<name>``, and the original's sha256 must be unchanged."""
    src = Path(src).resolve()
    out = Path(out_dir).resolve()
    if src.parent == out:
        raise UnpackError("refusing to unpack into the folder that holds the original")
    before = sha256_path(src)
    (out / "input").mkdir(parents=True, exist_ok=True)
    copy = out / "input" / src.name
    shutil.copyfile(src, copy)
    dest = out / src.name
    if dest.exists():
        dest.unlink()
    try:
        r = subprocess.run([str(upx.exe), "-d", "-q", "-o", str(dest), str(copy)], capture_output=True, text=True,
                           timeout=timeout, cwd=str(out))
    except subprocess.TimeoutExpired:
        raise UnpackError(f"upx -d timed out after {timeout:.0f}s") from None
    after = sha256_path(src)
    if after != before:   # cannot happen with -o on a copy; checked anyway
        raise UnpackError("the original file changed during unpacking")
    if r.returncode != 0 or not dest.is_file():
        raise UnpackError(f"upx -d failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")
    rep = packer_report(dest)
    return {"ok": True, "tool": "upx", "tool_version": upx.version, "tool_sha256": upx.sha256, "tool_pinned": upx.pinned,
            "original_sha256": before, "unpacked_path": str(dest), "unpacked_sha256": sha256_path(dest),
            "unpacked_size": dest.stat().st_size, "still_packed": rep["packed"], "unpacked_report": rep}


def prepare_for_analysis(path: Path | str, work_dir: Path | str, *, allow_unpack: bool,
                         tools_dir: Path | str | None = None) -> tuple[Path, dict[str, Any]]:
    """Packer check, then (only with consent and a pinned unpacker) unpack into ``work_dir``.

    Returns ``(path_to_analyse, info)``; ``info`` = ``{"report", "unpack": None | result | {"ok": False, "reason"}}``.
    Without consent, without a known unpacker or when unpacking fails, the original path is returned unchanged.
    """
    p = Path(path)
    rep = packer_report(p)
    info: dict[str, Any] = {"report": rep, "unpack": None}
    if not rep["packed"]:
        return p, info
    if rep["packer"] != "UPX":
        info["unpack"] = {"ok": False, "reason": f"no pinned unpacker for {rep['packer']}; analysing the packed file as is"}
        return p, info
    if not allow_unpack:
        info["unpack"] = {"ok": False, "reason": "packed with UPX; unpacking needs your permission (it runs upx -d on a copy)",
                          "needs_consent": True}
        return p, info
    upx = find_upx(tools_dir)
    if upx is None:
        info["unpack"] = {"ok": False, "reason": "UPX is not installed (Tools page: UPX)", "needs_tool": "upx"}
        return p, info
    try:
        res = unpack_upx(p, work_dir, upx)
    except (UnpackError, OSError) as e:
        info["unpack"] = {"ok": False, "reason": str(e)[:400]}
        return p, info
    info["unpack"] = res
    return Path(res["unpacked_path"]), info
