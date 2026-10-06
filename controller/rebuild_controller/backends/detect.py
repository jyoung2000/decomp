"""Format/profile detection for modules found in an installation root.

Returns a Detection with format (pe|elf|macho|dotnet|godot_pck|asar|js_bundle|wasm|jar|apk|aab|dex|java_class|gamemaker_data|
unreal_pak|unreal_iostore|il2cpp_metadata|archive|data|text|unknown), profile
(native_pe|native_elf|native_macho|dotnet|unity_mono|unity_il2cpp|godot|gamemaker|electron|web|android|jvm|unreal|unknown), flags and
short ``evidence`` strings. Detection is evidence, not a parity claim.

Every detection whose profile is tracked in ``support.SUPPORT`` exposes ``support`` (the full plain-language record, including the
``statement`` shown before work starts) and ``to_dict()`` carries ``support_status`` (supported|partial|detected_only|unsupported). The
installation-level summary (``summarize_profile``) carries the full support record for the primary profile.
"""
from __future__ import annotations

import json
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import support as _support

MAGIC_SNIFF = 0x1000
ZIP_NAME_CAP = 200_000
UNITY_METADATA_MAGIC = b"\xaf\x1b\xb1\xfa"
GM_DATA_NAMES = {"data.win", "game.unx", "game.ios", "game.droid"}
GM_RUNNER_HINTS = ("runner", "gamemaker")


@dataclass
class Detection:
    format: str
    profile: str
    arch: str | None = None
    bits: int | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)

    @property
    def support(self) -> dict[str, Any] | None:
        return _support.support_for(self.profile, self.flags)

    @property
    def support_status(self) -> str | None:
        s = _support.SUPPORT.get(self.profile)
        return s["status"] if s else None

    def to_dict(self) -> dict[str, Any]:
        d = {"format": self.format, "profile": self.profile, "arch": self.arch, "bits": self.bits,
             "flags": self.flags, "confidence": self.confidence}
        if self.evidence:
            d["evidence"] = self.evidence
        if self.support_status:
            d["support_status"] = self.support_status
        return d


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
        return _detect_elf(head, name)
    mk = _support.macho_kind(head)
    if mk is not None:
        return _detect_macho(path, head, mk, name)
    if _support.is_java_class(head):
        return _detect_class(head)
    if head.startswith(b"GDPC"):
        return _detect_pck(head)
    if head.startswith(b"\x00asm"):
        return Detection("wasm", "web", flags={"wasm": True})
    if name.endswith(".asar") and len(head) >= 16:
        return Detection("asar", "electron", flags={"asar": True})
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return _detect_zip(path, name)
    if head[:4] == b"dex\n" and head[7:8] == b"\0":
        return Detection("dex", "android", flags={"dex_version": head[4:7].decode("ascii", "replace")}, evidence=["dex header"])
    if _support.is_gamemaker_form(head) or (head.startswith(b"FORM") and name in GM_DATA_NAMES):
        return _detect_gamemaker(head, name)
    if head.startswith(b"FORM") and name.startswith("audiogroup") and head[8:12] == b"AUDO":
        return Detection("gamemaker_data", "gamemaker", flags={"gamemaker_audio_group": True}, evidence=["FORM/AUDO audio group"])
    if name.endswith(".utoc") and head.startswith(_support.UTOC_MAGIC):
        return Detection("unreal_iostore", "unreal", flags={"iostore": True, "utoc": True}, evidence=["IoStore .utoc magic"])
    if name.endswith(".pak"):
        d = _detect_pak(path)
        if d is not None:
            return d
    if name == "global-metadata.dat" or head.startswith(UNITY_METADATA_MAGIC):
        flags: dict[str, Any] = {"il2cpp_metadata": True}
        if head.startswith(UNITY_METADATA_MAGIC) and len(head) >= 8:
            flags["metadata_version"] = struct.unpack("<i", head[4:8])[0]
        else:
            flags["metadata_header_invalid"] = True   # named global-metadata.dat but sanity header absent: encrypted/obfuscated?
        return Detection("il2cpp_metadata", "unity_il2cpp", flags=flags, confidence=1.0 if "metadata_version" in flags else 0.6,
                         evidence=["global-metadata.dat" + (" (sanity header ok)" if "metadata_version" in flags else " (sanity header missing)")])
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


def detect_path(path: Path | str) -> dict[str, Any]:
    """Detection plus the full support record (with plain-language statement) for one file."""
    d = sniff(Path(path))
    out = d.to_dict()
    out["support"] = d.support
    return out


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
    lname = path.name.lower()
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
            if lname.startswith("assembly-csharp") or lname.startswith("unityengine"):
                prof = "unity_mono"
            return Detection("dotnet", prof, arch=arch, bits=bits, flags=flags | {"clr": True},
                             evidence=["CLR header"] + (["Unity assembly name"] if prof == "unity_mono" else []))
        if lname == "gameassembly.dll":
            return Detection("pe", "unity_il2cpp", arch=arch, bits=bits, flags=flags, evidence=["GameAssembly.dll (IL2CPP native game code)"])
        if lname == "unityplayer.dll":
            flags["engine_binary"] = "unity"          # engine, not game code: consumers may skip it
            return Detection("pe", "native_pe", arch=arch, bits=bits, flags=flags, evidence=["UnityPlayer.dll (Unity engine)"])
        if lname.endswith("-shipping.exe"):
            flags["unreal_shipping"] = True
        if lname in ("electron.exe",) or "electron" in lname:
            flags["electron_host"] = True
        return Detection("pe", "native_pe", arch=arch, bits=bits, flags=flags)
    except Exception as e:  # malformed PE: still a PE by magic, lower confidence
        flags["parse_error"] = f"{type(e).__name__}: {e}"[:200]
        if lname == "gameassembly.dll":
            return Detection("pe", "unity_il2cpp", flags=flags, confidence=0.5, evidence=["GameAssembly.dll by name (PE header unreadable)"])
        if lname == "unityplayer.dll":
            flags["engine_binary"] = "unity"
        return Detection("pe", "native_pe", flags=flags, confidence=0.5)


def _detect_elf(head: bytes, name: str = "") -> Detection:
    bits = 64 if head[4] == 2 else 32
    machine = struct.unpack("<H" if head[5] == 1 else ">H", head[18:20])[0]
    arch = {0x3E: "x86_64", 0x03: "x86", 0xB7: "arm64", 0x28: "arm", 0xF3: "riscv"}.get(machine, hex(machine))
    etype = struct.unpack("<H" if head[5] == 1 else ">H", head[16:18])[0]
    flags = {"type": {2: "exec", 3: "dyn"}.get(etype, str(etype))}
    if name in ("gameassembly.so", "libil2cpp.so"):
        return Detection("elf", "unity_il2cpp", arch=arch, bits=bits, flags=flags, evidence=[f"{name} (IL2CPP native game code)"])
    return Detection("elf", "native_elf", arch=arch, bits=bits, flags=flags)


def _detect_macho(path: Path, head: bytes, kind: str, name: str) -> Detection:
    flags: dict[str, Any] = {"layout": kind}
    arch, bits = None, None
    try:
        info = _support.inspect_macho(path)
        flags.update({"slice_count": info["slice_count"], "architectures": info["architectures"], "encrypted": info["encrypted"]})
        first = info["slices"][0] if info["slices"] else {}
        flags["filetype"] = first.get("filetype")
        flags["platform"] = first.get("platform")
        if info["slice_count"] == 1:
            arch, bits = first.get("arch"), first.get("bits")
        else:
            arch = "universal:" + "+".join(a for a in info["architectures"] if a)
    except (OSError, ValueError, struct.error) as e:
        flags["parse_error"] = f"{type(e).__name__}: {e}"[:200]
    prof = "unity_il2cpp" if name == "gameassembly.dylib" else "native_macho"
    ev = ["Mach-O fat/universal header" if kind == "fat" else "Mach-O header"]
    return Detection("macho", prof, arch=arch, bits=bits, flags=flags, evidence=ev, confidence=1.0 if "parse_error" not in flags else 0.7)


def _detect_class(head: bytes) -> Detection:
    minor, major = struct.unpack(">HH", head[4:8])
    return Detection("java_class", "jvm", flags={"class_major": major, "class_minor": minor, "java_version": major - 44 if major >= 45 else None},
                     evidence=[f"class file magic, major {major}"])


def _detect_pck(head: bytes) -> Detection:
    try:
        ver, major, minor, patch = struct.unpack("<iiii", head[4:20])
        return Detection("godot_pck", "godot", flags={"pack_version": ver, "engine": f"{major}.{minor}.{patch}"})
    except struct.error:
        return Detection("godot_pck", "godot", confidence=0.6)


def _detect_gamemaker(head: bytes, name: str) -> Detection:
    flags: dict[str, Any] = {"gamemaker_data": True}
    ev = ["FORM/GEN8 IFF header"] if _support.is_gamemaker_form(head) else [f"{name} (FORM header without GEN8 in the first bytes)"]
    if _support.is_gamemaker_form(head) and len(head) >= 18:
        flags["bytecode_version"] = head[17]
    return Detection("gamemaker_data", "gamemaker", flags=flags, evidence=ev, confidence=1.0 if _support.is_gamemaker_form(head) else 0.7)


def _detect_pak(path: Path) -> Detection | None:
    try:
        foot = _support.read_pak_footer(path)
    except OSError:
        return None
    if foot is None:
        return None
    flags = {"pak": True, "pak_version": foot["pak_version"]}
    if "encrypted_index" in foot:
        flags["encrypted_index"] = foot["encrypted_index"]
    return Detection("unreal_pak", "unreal", flags=flags, evidence=[f"pak footer magic 0x5A6F12E1, pak version {foot['pak_version']}"])


def _detect_zip(path: Path, name: str) -> Detection:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        prof = "android" if name.endswith((".apk", ".aab")) else "jvm" if name.endswith(".jar") else "unknown"
        return Detection("archive", prof, flags={"zip": True, "zip_error": f"{type(e).__name__}: {e}"[:200]}, confidence=0.4)
    with zf:
        names = zf.namelist()[:ZIP_NAME_CAP]
        nset = set(names)
        dex = sorted(n for n in names if "/" not in n and n.startswith("classes") and n.endswith(".dex"))
        is_aab = "base/manifest/AndroidManifest.xml" in nset or "BundleConfig.pb" in nset
        if is_aab:
            bdex = sorted(n for n in names if n.startswith("base/dex/") and n.endswith(".dex"))
            return Detection("aab", "android", flags={"zip": True, "aab": True, "dex_files": len(bdex)},
                             evidence=["BundleConfig.pb / base/manifest/AndroidManifest.xml"] + bdex[:3])
        if "AndroidManifest.xml" in nset:
            flags: dict[str, Any] = {"zip": True, "apk": True, "dex_files": len(dex)}
            libs = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2})
            if libs:
                flags["abis"] = libs
            low = {n.rsplit("/", 1)[-1] for n in names if n.startswith("lib/")}
            fw = sorted({lbl for fn, lbl in (("libflutter.so", "flutter"), ("libunity.so", "unity"), ("libil2cpp.so", "unity-il2cpp"),
                                             ("libhermes.so", "react-native"), ("libmonodroid.so", "xamarin")) if fn in low})
            if fw:
                flags["frameworks"] = fw
            ev = ["AndroidManifest.xml"] + dex[:3]
            return Detection("apk", "android", flags=flags, evidence=ev, confidence=1.0 if dex else 0.8)
        classes = [n for n in names if n.endswith(".class")]
        has_mf = any(n.upper() == "META-INF/MANIFEST.MF" for n in names)
        if classes or (has_mf and name.endswith((".jar", ".war", ".ear"))):
            flags = {"zip": True, "jar": True, "class_count": len(classes), "manifest": has_mf}
            ev = (["META-INF/MANIFEST.MF"] if has_mf else []) + (["*.class (%d)" % len(classes)] if classes else [])
            if has_mf:
                try:
                    mi = zf.getinfo("META-INF/MANIFEST.MF")
                    if mi.file_size <= 65536:
                        for ln in zf.read(mi).decode("utf-8", "replace").splitlines():
                            if ln.startswith("Main-Class:"):
                                flags["main_class"] = ln.split(":", 1)[1].strip()
                except (KeyError, OSError):
                    pass
            if any(n.startswith("BOOT-INF/classes/") for n in names):
                flags["spring_boot"] = True
            if any(n.startswith("WEB-INF/classes/") for n in names):
                flags["war"] = True
            return Detection("jar", "jvm", flags=flags, evidence=ev, confidence=1.0 if classes else 0.7)
        if name.endswith(".apk"):
            return Detection("archive", "android", flags={"zip": True, "apk": True, "no_manifest": True}, confidence=0.4)
        if name.endswith(".jar"):
            return Detection("archive", "jvm", flags={"zip": True, "jar": True, "no_classes": True}, confidence=0.4)
    return Detection("archive", "unknown", flags={"zip": True})


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


def _unreal_version(root: Path | None, rels: list[str]) -> dict[str, Any] | None:
    if root is None:
        return None
    low = {r.lower(): r for r in rels}
    bv = next((r for lr, r in low.items() if lr.endswith("engine/build/build.version")), None)
    if bv:
        try:
            j = json.loads((root / bv).read_text(encoding="utf-8", errors="replace"))
            return {"engine": f"UE{j.get('MajorVersion')}", "version": f"{j.get('MajorVersion')}.{j.get('MinorVersion')}.{j.get('PatchVersion')}",
                    "branch": j.get("BranchName"), "source": bv}
        except (OSError, ValueError):
            pass
    exe = next((r for lr, r in low.items() if lr.endswith("-shipping.exe")), None)
    if exe:
        try:
            v = _support.unreal_version_from_exe(root / exe)
            v["source"] = v.get("source") or exe
            return v
        except OSError:
            return None
    return None


def summarize_profile(detections: list[tuple[str, Detection]], root: Path | None = None) -> dict[str, Any]:
    """Aggregate per-file detections into an installation-level profile with evidence and the support statement.

    Precision rules: IL2CPP needs a global-metadata.dat (or GameAssembly) while Mono Unity needs Managed/Assembly-CSharp*.dll, so a Mono
    game is never labelled IL2CPP and is routed to ILSpy via the ``unity_mono`` profile. Evidence lists the concrete files used."""
    counts: dict[str, int] = {}
    reasons: list[str] = []
    evidence: list[dict[str, str]] = []
    for rel, d in detections:
        if d.profile == "unknown":
            continue
        counts[d.profile] = counts.get(d.profile, 0) + 1
    rels = [rel for rel, _ in detections]
    names = {rel.lower().rsplit("/", 1)[-1] for rel in rels}
    unity = _support.unity_layout(rels)
    flags: dict[str, Any] = {}

    def ev(profile: str, kind: str, items: list[str], limit: int = 5) -> None:
        for it in items[:limit]:
            evidence.append({"profile": profile, "kind": kind, "path": it})

    gm_data = [r for r in rels if r.lower().rsplit("/", 1)[-1] in GM_DATA_NAMES]
    paks = [r for r, d in detections if d.format in ("unreal_pak", "unreal_iostore")]
    shipping = [r for r in rels if r.lower().endswith("-shipping.exe")]
    ue_bin = [r for r in rels if "engine/binaries/" in r.lower()]
    apks = [r for r, d in detections if d.format in ("apk", "aab")]
    jars = [r for r, d in detections if d.format == "jar"]
    classes = [r for r, d in detections if d.format == "java_class"]
    machos = [r for r, d in detections if d.format == "macho" and d.profile == "native_macho"]
    confidence = 1.0
    primary = "unknown"
    if "unity_il2cpp" in counts or unity["game_assembly"] or unity["il2cpp_metadata"]:
        primary = "unity_il2cpp"
        if unity["il2cpp_metadata"] and (unity["game_assembly"] or "unity_il2cpp" in counts):
            reasons.append("GameAssembly + global-metadata.dat present (IL2CPP)")
        elif unity["il2cpp_metadata"]:
            reasons.append("global-metadata.dat present without a GameAssembly binary in the scanned tree"); confidence = 0.8
        else:
            reasons.append("GameAssembly present but global-metadata.dat not found (moved, packed or encrypted)"); confidence = 0.7
        ev(primary, "il2cpp_metadata", unity["il2cpp_metadata"]); ev(primary, "game_assembly", unity["game_assembly"]); ev(primary, "unity_player", unity["unity_player"], 1)
    elif "unity_mono" in counts or unity["managed_assemblies"]:
        primary = "unity_mono"
        reasons.append("Managed/Assembly-CSharp*.dll present (Mono scripting backend; recoverable through ILSpy)")
        ev(primary, "managed_assembly", unity["managed_assemblies"] or [r for r, d in detections if d.profile == "unity_mono"]); ev(primary, "unity_player", unity["unity_player"], 1)
    elif "godot" in counts and any(rel.lower().endswith(".pck") for rel in rels):
        primary = "godot"; reasons.append("GDPC pack present")
        ev(primary, "pck", [r for r in rels if r.lower().endswith(".pck")])
    elif "gamemaker" in counts and gm_data:
        primary = "gamemaker"; reasons.append("data.win/game.unx (FORM/GEN8) present")
        ev(primary, "gamemaker_data", gm_data)
    elif "electron" in counts:
        primary = "electron"; reasons.append("app.asar present")
    elif apks:
        primary = "android"; reasons.append("APK/AAB present")
        ev(primary, "package", apks)
        for r, d in detections:
            if d.format in ("apk", "aab") and d.flags.get("frameworks"):
                flags["frameworks"] = d.flags["frameworks"]
            if d.format == "aab":
                flags["aab"] = True
    elif paks or ((shipping or ue_bin) and "native_pe" in counts):
        primary = "unreal"
        reasons.append("UE pak/IoStore present" if paks else "Unreal Shipping executable / Engine/Binaries present without paks")
        if not paks:
            confidence = 0.6
        ev(primary, "pak", paks); ev(primary, "shipping_exe", shipping, 2); ev(primary, "engine_binaries", ue_bin, 2)
        ver = _unreal_version(root, rels)
        if ver:
            flags["engine_version"] = ver
            if ver.get("engine"):
                reasons.append(f"engine marker {ver['engine']} {ver.get('version') or ''}".strip())
    elif jars or classes:
        primary = "jvm"; reasons.append("jar archive present" if jars else "loose .class files present")
        ev(primary, "jar", jars or classes)
        if any(d.flags.get("spring_boot") for _, d in detections if d.format == "jar"):
            flags["spring_boot"] = True
    elif "dotnet" in counts and counts.get("native_pe", 0) <= counts["dotnet"]:
        primary = "dotnet"; reasons.append("CLR header in PE")
    elif "native_pe" in counts:
        primary = "native_pe"; reasons.append("native PE executables present")
    elif "native_elf" in counts:
        primary = "native_elf"; reasons.append("ELF executables present")
    elif machos:
        primary = "native_macho"; reasons.append("Mach-O binaries present")
        ev(primary, "macho", machos)
    elif "web" in counts and any(n in names for n in ("index.html", "manifest.webmanifest", "package.json")):
        primary = "web"; reasons.append("HTML/JS site present")
    secondary = sorted(p for p in counts if p not in (primary, "native_pe", "native_elf", "dotnet", "web") and counts[p] and p in _support.SUPPORT)
    sup = _support.support_for(primary, flags) if primary != "unknown" else None
    out: dict[str, Any] = {"primary": primary, "counts": counts, "reasons": reasons, "evidence": evidence, "confidence": confidence,
                           "secondary": secondary, "support": sup, "support_status": sup["status"] if sup else None,
                           "support_statement": sup["statement"] if sup else None}
    if flags.get("engine_version"):
        out["engine_version"] = flags["engine_version"]
    if unity["has_unity_markers"]:
        out["unity"] = {k: v[:5] if isinstance(v, list) else v for k, v in unity.items()}
    return out
