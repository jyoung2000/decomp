"""Format/profile detection for modules found in an installation root.

Returns a Detection with format (pe|elf|macho|dotnet|godot_pck|asar|js_bundle|wasm|archive|data|text|unknown),
profile (native_pe|native_elf|dotnet|unity_mono|unity_il2cpp|godot|gamemaker|electron|web|android|unreal|unknown)
and flags. Detection is evidence, not a parity claim.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAGIC_SNIFF = 0x1000


@dataclass
class Detection:
    format: str
    profile: str
    arch: str | None = None
    bits: int | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"format": self.format, "profile": self.profile, "arch": self.arch, "bits": self.bits,
                "flags": self.flags, "confidence": self.confidence}


def sniff(path: Path, head: bytes | None = None) -> Detection:
    if head is None:
        try:
            with open(path, "rb") as f:
                head = f.read(MAGIC_SNIFF)
        except OSError as e:
            return Detection("unknown", "unknown", flags={"error": str(e)}, confidence=0.0)
    name = path.name.lower()
    if head.startswith(b"MZ"):
        return _detect_pe(path, head)
    if head.startswith(b"\x7fELF"):
        return _detect_elf(head)
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
        return Detection("macho", "native_macho")
    if head.startswith(b"GDPC"):
        return _detect_pck(head)
    if head.startswith(b"\x00asm"):
        return Detection("wasm", "web", flags={"wasm": True})
    if name.endswith(".asar") and len(head) >= 16:
        return Detection("asar", "electron", flags={"asar": True})
    if head.startswith(b"PK\x03\x04"):
        if name.endswith(".apk"):
            return Detection("archive", "android", flags={"zip": True, "apk": True})
        if name.endswith(".jar"):
            return Detection("archive", "jvm", flags={"zip": True, "jar": True})
        return Detection("archive", "unknown", flags={"zip": True})
    if name == "data.win" or head.startswith(b"FORM") and b"GEN8" in head[:64]:
        return Detection("data", "gamemaker", flags={"gamemaker_data": True})
    if name.endswith(".pak") and head[:4] == b"\xe1\x12\x6f\x5a"[::-1]:
        return Detection("archive", "unreal", flags={"pak": True})
    if name == "global-metadata.dat" or head.startswith(b"\xaf\x1b\xb1\xfa"):
        return Detection("data", "unity_il2cpp", flags={"il2cpp_metadata": True})
    if name.endswith((".js", ".mjs", ".cjs")):
        return Detection("js_bundle", "web", flags=_js_flags(head))
    if name.endswith((".html", ".htm")):
        return Detection("text", "web", flags={"html": True})
    if name in ("manifest.webmanifest", "manifest.json") or name.endswith(".webmanifest"):
        return Detection("text", "web", flags={"webmanifest": True})
    if name == "package.json":
        return Detection("text", "web", flags={"package_json": True})
    if name == "project.godot" or name.endswith((".tscn", ".gd", ".tres")):
        return Detection("text", "godot", flags={"godot_text": True})
    if _looks_text(head):
        return Detection("text", "unknown", confidence=0.5)
    return Detection("data", "unknown", confidence=0.3)


def _looks_text(head: bytes) -> bool:
    if not head:
        return True
    sample = head[:512]
    textchars = bytes(range(32, 127)) + b"\n\r\t\b\f"
    return sum(c in textchars for c in sample) / len(sample) > 0.9


def _js_flags(head: bytes) -> dict[str, Any]:
    h = head[:MAGIC_SNIFF]
    return {
        "webpack": b"__webpack_require__" in h or b"webpackChunk" in h,
        "vite": b"__vite" in h or b"vite/modulepreload" in h,
        "esbuild": b"__esbuild" in h or b"__toESM" in h,
        "source_map": b"sourceMappingURL" in h,
        "service_worker": b"self.addEventListener" in h and (b"fetch" in h or b"install" in h),
    }


def _detect_pe(path: Path, head: bytes) -> Detection:
    flags: dict[str, Any] = {}
    arch, bits = None, None
    try:
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        machine = pe.FILE_HEADER.Machine
        arch = {0x14C: "x86", 0x8664: "x86_64", 0xAA64: "arm64", 0x1C0: "arm"}.get(machine, hex(machine))
        bits = 64 if machine in (0x8664, 0xAA64) else 32
        flags["subsystem"] = {2: "gui", 3: "console"}.get(pe.OPTIONAL_HEADER.Subsystem, str(pe.OPTIONAL_HEADER.Subsystem))
        flags["dll"] = bool(pe.FILE_HEADER.Characteristics & 0x2000)
        clr = getattr(pe, "DIRECTORY_ENTRY_COM_DESCRIPTOR", None)
        com_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14] if len(pe.OPTIONAL_HEADER.DATA_DIRECTORY) > 14 else None
        is_dotnet = bool(clr) or bool(com_dir and com_dir.VirtualAddress and com_dir.Size)
        imports = []
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
            imports.append(entry.dll.decode("latin-1").lower())
        flags["imports"] = imports[:64]
        pe.close()
        if is_dotnet:
            prof = "dotnet"
            n = path.name.lower()
            if n in ("assembly-csharp.dll", "unityengine.dll") or n.startswith("unityengine"):
                prof = "unity_mono"
            return Detection("dotnet", prof, arch=arch, bits=bits, flags=flags | {"clr": True})
        if path.name.lower() == "gameassembly.dll":
            return Detection("pe", "unity_il2cpp", arch=arch, bits=bits, flags=flags)
        if path.name.lower() in ("electron.exe",) or "electron" in path.name.lower():
            flags["electron_host"] = True
        return Detection("pe", "native_pe", arch=arch, bits=bits, flags=flags)
    except Exception as e:  # malformed PE: still a PE by magic, lower confidence
        flags["parse_error"] = f"{type(e).__name__}: {e}"[:200]
        return Detection("pe", "native_pe", flags=flags, confidence=0.5)


def _detect_elf(head: bytes) -> Detection:
    bits = 64 if head[4] == 2 else 32
    machine = struct.unpack("<H" if head[5] == 1 else ">H", head[18:20])[0]
    arch = {0x3E: "x86_64", 0x03: "x86", 0xB7: "arm64", 0x28: "arm", 0xF3: "riscv"}.get(machine, hex(machine))
    etype = struct.unpack("<H" if head[5] == 1 else ">H", head[16:18])[0]
    return Detection("elf", "native_elf", arch=arch, bits=bits, flags={"type": {2: "exec", 3: "dyn"}.get(etype, str(etype))})


def _detect_pck(head: bytes) -> Detection:
    try:
        ver, major, minor, patch = struct.unpack("<iiii", head[4:20])
        return Detection("godot_pck", "godot", flags={"pack_version": ver, "engine": f"{major}.{minor}.{patch}"})
    except struct.error:
        return Detection("godot_pck", "godot", confidence=0.6)


def embedded_pck_offset(path: Path) -> int | None:
    """Godot executables may embed a PCK at the end: trailer = int64 size + 'GDPC'."""
    try:
        size = path.stat().st_size
        if size < 12:
            return None
        with open(path, "rb") as f:
            f.seek(size - 12)
            tail = f.read(12)
        if tail[8:12] != b"GDPC":
            return None
        pck_size = struct.unpack("<q", tail[:8])[0]
        off = size - 12 - pck_size
        if off < 0:
            return None
        with open(path, "rb") as f:
            f.seek(off)
            return off if f.read(4) == b"GDPC" else None
    except OSError:
        return None


def summarize_profile(detections: list[tuple[str, Detection]]) -> dict[str, Any]:
    """Aggregate per-file detections into an installation-level profile with evidence."""
    counts: dict[str, int] = {}
    reasons: list[str] = []
    for rel, d in detections:
        if d.profile == "unknown":
            continue
        counts[d.profile] = counts.get(d.profile, 0) + 1
    names = {rel.lower().rsplit("/", 1)[-1] for rel, _ in detections}
    primary = "unknown"
    if "unity_il2cpp" in counts or "gameassembly.dll" in names:
        primary = "unity_il2cpp"; reasons.append("GameAssembly.dll / global-metadata.dat present")
    elif "unity_mono" in counts:
        primary = "unity_mono"; reasons.append("Assembly-CSharp.dll present")
    elif "godot" in counts and any(rel.lower().endswith(".pck") for rel, _ in detections):
        primary = "godot"; reasons.append("GDPC pack present")
    elif "gamemaker" in counts:
        primary = "gamemaker"; reasons.append("data.win present")
    elif "electron" in counts:
        primary = "electron"; reasons.append("app.asar present")
    elif "android" in counts:
        primary = "android"; reasons.append("APK present")
    elif "unreal" in counts:
        primary = "unreal"; reasons.append("UE pak present")
    elif "dotnet" in counts and counts.get("native_pe", 0) <= counts["dotnet"]:
        primary = "dotnet"; reasons.append("CLR header in PE")
    elif "native_pe" in counts:
        primary = "native_pe"; reasons.append("native PE executables present")
    elif "native_elf" in counts:
        primary = "native_elf"; reasons.append("ELF executables present")
    elif "web" in counts and any(n in names for n in ("index.html", "manifest.webmanifest", "package.json")):
        primary = "web"; reasons.append("HTML/JS site present")
    return {"primary": primary, "counts": counts, "reasons": reasons}
