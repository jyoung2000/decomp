"""Support statements and bounded inspectors for input kinds that are NOT fully recoverable (or only partly).

Two things live here and both are deliberately free of external tools:

* ``SUPPORT`` / ``support_for``: the plain-language statement every detection result carries. It is shown BEFORE any work starts and says
  what can be recovered, what cannot, what the rebuild will be and what the next step is when a kind is not supported.
* ``inspect_*``: bounded, read-only header/chunk parsers (Mach-O, GameMaker data.win, Unreal pak/utoc, Unity IL2CPP global-metadata.dat).
  They return what a user can still get for kinds where code recovery is unsupported (identifier names, chunk tables, footers,
  architectures, encryption flags). Every list is capped and reports ``truncated``; nothing here claims code recovery.

Statuses: ``supported`` (a pinned tool recovers code and a fixture regression passed), ``partial`` (a tool or parser recovers part of
the material), ``detected_only`` (identified with evidence, inventory only), ``unsupported`` (nothing is offered).
"""
from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from typing import Any

LIST_CAP = 1000
STATUS_LABEL = {"supported": "Supported", "partial": "Partial", "detected_only": "Detected only", "unsupported": "Unsupported"}


def _tool(name: str, license: str, url: str, note: str) -> dict[str, str]:
    return {"name": name, "license": license, "url": url, "note": note}


# License strings are as published upstream; they are recorded for planning only and MUST be re-verified before a tool is adopted.
FUTURE_TOOLS: dict[str, list[dict[str, str]]] = {
    "unity_il2cpp": [
        _tool("Il2CppDumper", "MIT", "https://github.com/Perfare/Il2CppDumper", "dumps types/method signatures and addresses from GameAssembly + global-metadata.dat; no method bodies"),
        _tool("Cpp2IL", "MIT", "https://github.com/SamboyCoding/Cpp2IL", "IL2CPP metadata + ISIL lifting; bodies are approximate"),
        _tool("AssetRipper", "GPL-3.0", "https://github.com/AssetRipper/AssetRipper", "Unity asset extraction; GPL: run as a separate process only, never link"),
    ],
    "gamemaker": [
        _tool("UndertaleModTool (UTMT)", "GPL-3.0", "https://github.com/UnderminersTeam/UndertaleModTool", "data.win decompile (VM builds); GPL: separate process only"),
    ],
    "unreal": [
        _tool("CUE4Parse", "Apache-2.0", "https://github.com/FabianFG/CUE4Parse", "pak/IoStore reader and asset deserializer (library; needs the game's AES key when encrypted)"),
        _tool("FModel", "GPL-3.0", "https://github.com/4sval/FModel", "GUI asset browser built on CUE4Parse; interactive, not scriptable enough for the pipeline"),
    ],
    "native_macho": [
        _tool("Rizin", "LGPL-3.0", "https://github.com/rizinorg/rizin", "already pinned for PE/ELF; Mach-O support exists upstream but has no fixture or regression in this repo"),
        _tool("LIEF", "Apache-2.0", "https://github.com/lief-project/LIEF", "Mach-O/fat parsing library (already a dependency for PE inspection)"),
    ],
    "android": [
        _tool("Apktool", "Apache-2.0", "https://github.com/iBotPeaches/Apktool", "resources.arsc / res decoding and smali round trip"),
    ],
}

_NO_EQUIV = "Nothing recovered is claimed to behave like the original until the scenario comparator has run against it."

SUPPORT: dict[str, dict[str, Any]] = {
    "native_pe": {
        "title": "Native Windows executable (PE)", "status": "supported", "backend": "rizin",
        "can_recover": ["imports/exports, strings, functions and per-function pseudo-C (Rizin + rz-ghidra)"],
        "cannot_recover": ["original source, identifiers, comments and types", "behavior beyond what scenarios observe"],
        "rebuild": "A clean-room re-implementation in the chosen target language, guided by the pseudo-C and by recorded scenario behavior.",
        "verification": "fixture pecli (scenario oracle)", "blocker": "", "next_action": "",
    },
    "native_elf": {
        "title": "Native Linux executable (ELF)", "status": "supported", "backend": "rizin",
        "can_recover": ["imports/exports, strings, functions and per-function pseudo-C (Rizin + rz-ghidra)"],
        "cannot_recover": ["original source, identifiers, comments and types"],
        "rebuild": "A clean-room re-implementation in the chosen target language, guided by the pseudo-C and by recorded scenario behavior.",
        "verification": "tests/test_rizin.py", "blocker": "", "next_action": "",
    },
    "dotnet": {
        "title": ".NET assembly", "status": "supported", "backend": "ilspy",
        "can_recover": ["C# source per type from IL metadata (ILSpy), with a per-type recovery report"],
        "cannot_recover": ["original comments and exact formatting", "code that was obfuscated or encrypted (decompiles with error markers)"],
        "rebuild": "Target C# (Auto default): the recovered C# itself is rebuilt with the .NET SDK after deterministic fixes and judged by the scenario comparator; AI only repairs what still fails. A Rust port is optional.",
        "verification": "fixture dotnetapp (tests/test_ilspy.py)", "blocker": "", "next_action": "",
    },
    "unity_mono": {
        "title": "Unity (Mono scripting backend)", "status": "supported", "backend": "ilspy",
        "can_recover": ["C# game scripts from Managed/Assembly-CSharp*.dll (real IL), routed to ILSpy like any .NET assembly"],
        "cannot_recover": ["scenes, prefabs, textures, audio and other Unity assets (no asset extractor is bundled)",
                           "the Unity engine itself (it is not rebuilt)"],
        "rebuild": "The recovered scripts are reference code for a re-implementation; no Unity project is regenerated.",
        "verification": "tests/test_ilspy.py (Assembly-CSharp.dll + UnityEngine stub); no full Unity game fixture", "blocker": "", "next_action": "",
    },
    "unity_il2cpp": {
        "title": "Unity (IL2CPP scripting backend)", "status": "detected_only", "backend": "triage",
        "can_recover": ["evidence of the layout (GameAssembly + global-metadata.dat) and the metadata version",
                        "type/method/field identifier names listed from global-metadata.dat (bounded list)",
                        "native-code analysis of GameAssembly as an ordinary native module (no mapping back to C#)"],
        "cannot_recover": ["C# method bodies: IL2CPP compiled them to native code, so there is no IL to decompile",
                           "original C# source, comments and local names", "Unity assets (scenes/prefabs/textures)"],
        "rebuild": "None is produced from code. Any rebuild would be a clean-room re-implementation driven by recorded behavior; the plan marks it unsupported.",
        "verification": "tests/test_support_triage.py (synthetic headers only; no real IL2CPP game)",
        "blocker": "No IL2CPP dumper/lifter is integrated.",
        "next_action": "Use the identifier list and run scenario capture on the original; consider adding Il2CppDumper/Cpp2IL (see candidate tools).",
    },
    "gamemaker": {
        "title": "GameMaker", "status": "detected_only", "backend": "triage",
        "can_recover": ["chunk table of data.win/game.unx (GEN8, SPRT, SOND, CODE, STRG, TXTR, AUDO ...) with sizes",
                        "bytecode version and whether a CODE chunk exists (VM build) or not (likely YoYo Compiler native build)",
                        "string table entries (bounded list)"],
        "cannot_recover": ["GML scripts, room/object logic and decoded sprites/audio", "native-compiled (YYC) game logic"],
        "rebuild": "None is produced from code. Any rebuild would be a clean-room re-implementation driven by recorded behavior; the plan marks it unsupported.",
        "verification": "tests/test_support_triage.py (synthetic FORM/GEN8 sample; no real GameMaker game)",
        "blocker": "No data.win decompiler is integrated.",
        "next_action": "Capture scenarios from the running original; consider UndertaleModTool as an external process (GPL-3.0, see candidate tools).",
    },
    "android": {
        "title": "Android app (APK/AAB)", "status": "partial", "backend": "jvm",
        "can_recover": ["manifest facts: package, version, SDK levels, permissions, components, launcher activity (built-in AXML parser)",
                        "dex inventory: dex count, class and method counts, class names (built-in dex reader)",
                        "Java-like source for the dex code and decoded resources when jadx is installed (best effort)"],
        "cannot_recover": ["original Kotlin/Java source, comments and (when obfuscated) real names", "native libraries (lib/*.so) are listed, not decompiled",
                           "a rebuildable Android project or a signed APK"],
        "rebuild": "Reference Java sources only. The rebuild is a re-implementation in the chosen target and is judged by scenarios from the original; the APK itself is not repackaged.",
        "verification": "tests/test_jvm.py (jadx on a real minimal APK when installed; manifest/dex parsers always)",
        "blocker": "Code recovery needs jadx plus a Java runtime (both pinned in docs/dependency-lock.json); without them only manifest/dex evidence is produced.",
        "next_action": "Install the jadx and Temurin JRE entries from the dependency lock (Setup-Dependencies.ps1) and re-run recovery.",
    },
    "jvm": {
        "title": "Java/JVM (jar, class)", "status": "supported", "backend": "jvm",
        "can_recover": ["Java source per class from bytecode (CFR 0.152), with per-class recovery status and truncation disclosure",
                        "manifest, main class, class file versions, multi-release layout"],
        "cannot_recover": ["comments, original formatting and (when built without -g) real local variable names",
                           "readable code from obfuscated jars", "Kotlin/Scala/Groovy as the original language (output is Java-shaped and may not compile)"],
        "rebuild": "Target Java (Auto default): the recovered Java itself is recompiled with javac (--release from the class files) into a jar and judged by the scenario comparator against the original; AI only repairs what still fails.",
        "verification": "fixture javacli (tests/test_jvm.py: CFR decompile + javac recompile + 11/11 oracle scenarios)", "blocker": "",
        "next_action": "Needs a Java runtime for CFR: Temurin JRE from the dependency lock or any JDK/JRE 8+ on PATH.",
    },
    "unreal": {
        "title": "Unreal Engine", "status": "detected_only", "backend": "triage",
        "can_recover": ["engine generation (UE4/UE5) and version string when present in Build.version or the Shipping executable",
                        "pak footer facts: pak version, index offset/size, encrypted-index flag; IoStore (.utoc) presence",
                        "native analysis of the Shipping executable as an ordinary native module"],
        "cannot_recover": ["Blueprint graphs and game logic (cooked assets, not source)", "uasset/umap contents and extracted textures/audio/meshes",
                           "anything inside an encrypted pak without the AES key"],
        "rebuild": "None is produced from code. Any rebuild would be a clean-room re-implementation driven by recorded behavior; the plan marks it unsupported.",
        "verification": "tests/test_support_triage.py (synthetic pak footer; no real Unreal game)",
        "blocker": "No pak/IoStore extractor or uasset parser is integrated.",
        "next_action": "Capture scenarios from the running original; consider CUE4Parse as a library behind a new backend (Apache-2.0, see candidate tools).",
    },
    "native_macho": {
        "title": "macOS/iOS executable (Mach-O)", "status": "detected_only", "backend": "triage",
        "can_recover": ["architectures (including every slice of a fat/universal binary), file type, platform, linked dylibs",
                        "encryption flag (FairPlay-encrypted App Store binaries cannot be analysed)"],
        "cannot_recover": ["functions and pseudo-C: no Mach-O regression exists, so analysis is not offered", "Objective-C/Swift metadata and source"],
        "rebuild": "None is produced from code. Any rebuild would be a clean-room re-implementation driven by recorded behavior; the plan marks it unsupported.",
        "verification": "tests/test_support_triage.py (synthetic thin and fat headers)",
        "blocker": "No Mach-O fixture or verified analysis path; the host also cannot execute Mach-O binaries to capture behavior.",
        "next_action": "Provide a decrypted binary and a macOS/iOS runtime for scenario capture; Rizin/LIEF are candidates once a fixture exists.",
    },
    "godot": {
        "title": "Godot 4 (PCK)", "status": "supported", "backend": "gdre",
        "can_recover": ["project files, text scenes/resources and GDScript (when the bytecode version is supported) via GDRE tools"],
        "cannot_recover": ["the engine binary", "encrypted packs without the key", "C# assemblies outside the PCK (route them to ILSpy)"],
        "rebuild": "Recovered project is reference material; the rebuild is a re-implementation judged by scenarios.",
        "verification": "fixture godotgame (tests/test_gdre.py)", "blocker": "", "next_action": "",
    },
    "electron": {
        "title": "Electron/JS app", "status": "supported", "backend": "jsweb",
        "can_recover": ["app.asar contents and JS/HTML/CSS with source maps when present"],
        "cannot_recover": ["original TypeScript/JSX when source maps are absent", "native modules"],
        "rebuild": "A web/PWA re-implementation judged by the browser comparator.",
        "verification": "fixture webapp (tests/test_jsweb.py)", "blocker": "", "next_action": "",
    },
    "web": {
        "title": "Web app", "status": "supported", "backend": "jsweb",
        "can_recover": ["HTML/CSS/JS, manifests, service workers and source maps"],
        "cannot_recover": ["server-side code", "unminified names when no source map exists"],
        "rebuild": "A web/PWA re-implementation judged by the browser comparator.",
        "verification": "fixture webapp (tests/test_jsweb.py)", "blocker": "", "next_action": "",
    },
}


def support_for(profile: str, flags: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Full support record (with the plain-language ``statement``) for a profile, or None for unknown/untracked profiles."""
    base = SUPPORT.get(profile)
    if base is None:
        return None
    rec = {"profile": profile, **{k: (list(v) if isinstance(v, list) else v) for k, v in base.items()}}
    flags = flags or {}
    if profile == "android":
        if flags.get("aab"):
            rec["cannot_recover"].append("an .aab is a publishing bundle: base/ and feature modules are inspected, not installed")
        fw = flags.get("frameworks") or []
        if fw:
            rec["cannot_recover"].append("app logic written in " + "/".join(fw) + " (not Java: it lives in native or script bundles)")
    if profile == "jvm" and flags.get("spring_boot"):
        rec["can_recover"].append("application classes under BOOT-INF/classes (nested BOOT-INF/lib jars are listed, not decompiled)")
    rec["tools_candidate"] = FUTURE_TOOLS.get(profile, [])
    rec["statement"] = statement(rec)
    return rec


def statement(rec: dict[str, Any]) -> str:
    st = rec["status"]
    parts = [f"{rec['title']}: {STATUS_LABEL[st]}."]
    if rec["can_recover"]:
        parts.append("What can be recovered: " + "; ".join(rec["can_recover"]) + ".")
    if rec["cannot_recover"]:
        parts.append("What cannot: " + "; ".join(rec["cannot_recover"]) + ".")
    parts.append("What the rebuild will be: " + rec["rebuild"])
    if st in ("detected_only", "unsupported", "partial") and rec.get("blocker"):
        parts.append("Blocker: " + rec["blocker"])
    if rec.get("next_action"):
        parts.append("Next: " + rec["next_action"])
    parts.append(_NO_EQUIV)
    return " ".join(parts)


# =====================================================================================================================
# Mach-O
# =====================================================================================================================
_MACHO_THIN = {b"\xfe\xed\xfa\xce": (">", 32), b"\xfe\xed\xfa\xcf": (">", 64), b"\xce\xfa\xed\xfe": ("<", 32), b"\xcf\xfa\xed\xfe": ("<", 64)}
_MACHO_FAT = {b"\xca\xfe\xba\xbe": (">", False), b"\xca\xfe\xba\xbf": (">", True), b"\xbe\xba\xfe\xca": ("<", False), b"\xbf\xba\xfe\xca": ("<", True)}
_CPU = {7: "x86", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64", 0x0200000C: "arm64_32", 18: "ppc", 0x01000012: "ppc64"}
_FILETYPE = {1: "object", 2: "executable", 3: "fvmlib", 4: "core", 5: "preload", 6: "dylib", 7: "dylinker", 8: "bundle",
             9: "dylib_stub", 10: "dsym", 11: "kext"}
_PLATFORM = {1: "macos", 2: "ios", 3: "tvos", 4: "watchos", 5: "bridgeos", 6: "maccatalyst", 7: "ios-simulator", 8: "tvos-simulator",
             9: "watchos-simulator", 10: "driverkit", 11: "visionos"}
_LC_DYLIBS = {0xC, 0x80000018, 0x8000001F, 0x80000023}
_LC_ENCRYPTION = {0x21, 0x2C}


def macho_kind(head: bytes) -> str | None:
    """'thin', 'fat' or None. Disambiguates fat binaries from Java class files, which share 0xCAFEBABE: a class file stores
    minor/major version (major >= 45) where a fat header stores a small architecture count."""
    m = head[:4]
    if m in _MACHO_THIN:
        return "thin"
    if m in _MACHO_FAT:
        end = _MACHO_FAT[m][0]
        if len(head) >= 8:
            n = struct.unpack(end + "I", head[4:8])[0]
            if 1 <= n <= 30:
                return "fat"
        return None
    return None


def is_java_class(head: bytes) -> bool:
    if head[:4] != b"\xca\xfe\xba\xbe" or len(head) < 8:
        return False
    minor, major = struct.unpack(">HH", head[4:8])
    return major >= 45 and macho_kind(head) is None


def _macho_thin(f, base: int, size: int) -> dict[str, Any]:
    f.seek(base)
    h = f.read(32)
    end, bits = _MACHO_THIN[h[:4]]
    cputype, sub, ftype, ncmds, sizeofcmds, flags = struct.unpack(end + "iiIIII", h[4:28])
    out: dict[str, Any] = {"offset": base, "bits": bits, "endian": "big" if end == ">" else "little",
                           "arch": _CPU.get(cputype & 0xFFFFFFFF, hex(cputype & 0xFFFFFFFF)), "filetype": _FILETYPE.get(ftype, str(ftype)),
                           "flags": flags, "ncmds": ncmds, "dylibs": [], "encrypted": None, "platform": None, "uuid": None}
    pos = base + (32 if bits == 64 else 28)
    f.seek(pos)
    blob = f.read(min(sizeofcmds, 4 * 1024 * 1024))
    off = 0
    for _ in range(min(ncmds, 4000)):
        if off + 8 > len(blob):
            break
        cmd, cmdsize = struct.unpack_from(end + "II", blob, off)
        if cmdsize < 8 or off + cmdsize > len(blob):
            out["parse_note"] = "load commands truncated or malformed"
            break
        if cmd in _LC_DYLIBS and cmdsize > 24 and len(out["dylibs"]) < 200:
            nm = blob[off + struct.unpack_from(end + "I", blob, off + 8)[0]: off + cmdsize].split(b"\0", 1)[0]
            out["dylibs"].append(nm.decode("utf-8", "replace"))
        elif cmd in _LC_ENCRYPTION and cmdsize >= 20:
            out["encrypted"] = struct.unpack_from(end + "I", blob, off + 16)[0] != 0
        elif cmd == 0x32 and cmdsize >= 12:   # LC_BUILD_VERSION
            out["platform"] = _PLATFORM.get(struct.unpack_from(end + "I", blob, off + 8)[0], "other")
        elif cmd == 0x1B and cmdsize >= 24:   # LC_UUID
            out["uuid"] = blob[off + 8: off + 24].hex()
        off += cmdsize
    return out


def inspect_macho(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(4096)
        kind = macho_kind(head)
        if kind is None:
            raise ValueError("not a Mach-O file")
        slices: list[dict[str, Any]] = []
        if kind == "thin":
            if size < 28:
                raise ValueError("truncated Mach-O header")
            slices.append(_macho_thin(f, 0, size))
        else:
            end, is64 = _MACHO_FAT[head[:4]]
            n = struct.unpack(end + "I", head[4:8])[0]
            entry = 32 if is64 else 20
            for i in range(n):
                rec = head[8 + i * entry: 8 + (i + 1) * entry]
                if len(rec) < entry:
                    break
                if is64:
                    cpu, sub, off, sz = struct.unpack(end + "iiQQ", rec[:24])
                else:
                    cpu, sub, off, sz = struct.unpack(end + "iiII", rec[:16])
                entry_info: dict[str, Any] = {"declared_arch": _CPU.get(cpu & 0xFFFFFFFF, hex(cpu & 0xFFFFFFFF)), "offset": off, "size": sz}
                if off + 28 <= size:
                    f.seek(off)
                    if f.read(4) in _MACHO_THIN:
                        entry_info.update(_macho_thin(f, off, size))
                    else:
                        entry_info["parse_note"] = "slice header not Mach-O"
                else:
                    entry_info["parse_note"] = "slice offset outside file"
                slices.append(entry_info)
    enc = any(s.get("encrypted") for s in slices)
    return {"format": "macho", "layout": "fat" if kind == "fat" else "thin", "slice_count": len(slices), "slices": slices,
            "architectures": sorted({s.get("arch") or s.get("declared_arch") for s in slices}), "encrypted": enc,
            "can_still_get": ["architectures and slice table", "linked dylibs", "platform and encryption flag"],
            "blocker": ("binary is encrypted (LC_ENCRYPTION_INFO cryptid != 0): code is not readable until decrypted" if enc else
                        "no Mach-O analysis path is verified in this build")}


# =====================================================================================================================
# GameMaker data.win / game.unx
# =====================================================================================================================
def is_gamemaker_form(head: bytes) -> bool:
    return len(head) >= 12 and head[:4] == b"FORM" and head[8:12] == b"GEN8"


def inspect_gamemaker(path: Path | str, *, max_strings: int = LIST_CAP) -> dict[str, Any]:
    p = Path(path)
    size = p.stat().st_size
    chunks: list[dict[str, Any]] = []
    info: dict[str, Any] = {"format": "gamemaker_data", "size": size}
    with open(p, "rb") as f:
        head = f.read(12)
        if not is_gamemaker_form(head):
            raise ValueError("not a GameMaker IFF (FORM/GEN8) file")
        declared = struct.unpack("<I", head[4:8])[0] + 8
        end = min(declared, size)
        if declared != size:
            info["size_note"] = f"FORM length says {declared} bytes, file has {size}"
        pos = 8
        strg: tuple[int, int] | None = None
        while pos + 8 <= end and len(chunks) < 128:
            f.seek(pos)
            name, csize = struct.unpack("<4sI", f.read(8))
            nm = name.decode("latin-1")
            if not re.fullmatch(r"[A-Z0-9]{4}", nm) or pos + 8 + csize > size:
                info["chunk_note"] = f"chunk table stops at offset {pos}: invalid or out-of-range chunk"
                break
            chunks.append({"name": nm, "offset": pos + 8, "size": csize})
            if nm == "GEN8":
                g = f.read(min(csize, 4))
                if len(g) >= 2:
                    info["bytecode_version"] = g[1]
                    info["debug_disabled"] = bool(g[0])
            elif nm == "STRG":
                strg = (pos + 8, csize)
            pos += 8 + csize
        info["chunks"] = chunks
        names = {c["name"] for c in chunks}
        has_code = any(c["name"] == "CODE" and c["size"] > 8 for c in chunks)
        info["has_code_chunk"] = has_code
        info["build_kind"] = "vm_bytecode" if has_code else "likely_yyc_native_or_no_code"
        strings: list[str] = []
        truncated = False
        if strg and strg[1] >= 4:
            f.seek(strg[0])
            count = struct.unpack("<I", f.read(4))[0]
            info["string_count"] = count
            offs = struct.unpack(f"<{min(count, max_strings + 1)}I", f.read(4 * min(count, max_strings + 1))) if count else ()
            for o in offs[:max_strings]:
                if o + 4 > size:
                    continue
                f.seek(o)
                ln = struct.unpack("<I", f.read(4))[0]
                if ln > 4096 or o + 4 + ln > size:
                    continue
                strings.append(f.read(ln).decode("utf-8", "replace"))
            truncated = count > max_strings
        info["strings"] = strings
        info["strings_truncated"] = truncated
        info["chunk_names"] = sorted(names)
    info["can_still_get"] = ["chunk table", "bytecode version", "string table (bounded)"]
    info["blocker"] = "no GameMaker decompiler is integrated; GML and decoded sprites/audio are not produced"
    return info


# =====================================================================================================================
# Unreal pak / IoStore
# =====================================================================================================================
PAK_MAGIC = b"\xe1\x12\x6f\x5a"
UTOC_MAGIC = b"-==--==--==--==-"


def find_pak_footer(tail: bytes) -> dict[str, Any] | None:
    """Locate the pak footer magic (0x5A6F12E1 little-endian) in the file tail; the footer size depends on the pak version."""
    i = tail.rfind(PAK_MAGIC)
    if i < 0 or i + 8 > len(tail):
        return None
    version = struct.unpack_from("<I", tail, i + 4)[0]
    if not 1 <= version <= 12:
        return None
    rec: dict[str, Any] = {"pak_version": version, "footer_magic_from_end": len(tail) - i}
    if i + 24 <= len(tail):
        rec["index_offset"], rec["index_size"] = struct.unpack_from("<QQ", tail, i + 8)
    if version >= 4 and i >= 1:
        rec["encrypted_index"] = tail[i - 1] != 0
    return rec


def read_pak_footer(path: Path | str) -> dict[str, Any] | None:
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        f.seek(max(0, size - 400))
        return find_pak_footer(f.read(400))


_UE_VERSION_RE = re.compile(rb"\+\+UE([45])\+Release-(\d+\.\d+)")
UE_SCAN_LIMIT = 128 * 1024 * 1024


def unreal_version_from_exe(path: Path | str, *, limit: int = UE_SCAN_LIMIT) -> dict[str, Any]:
    """Scan (bounded) a Shipping executable for the ``++UE4+Release-4.27`` / ``++UE5+Release-5.3`` branch string."""
    p = Path(path)
    scanned = 0
    carry = b""
    with open(p, "rb") as f:
        while scanned < limit:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            scanned += len(chunk)
            m = _UE_VERSION_RE.search(carry + chunk)
            if m:
                return {"engine": f"UE{m.group(1).decode()}", "version": m.group(2).decode(), "source": f"branch string in {p.name}",
                        "scanned_bytes": scanned}
            carry = chunk[-64:]
    return {"engine": None, "version": None, "scanned_bytes": scanned, "scan_truncated": scanned >= limit,
            "source": f"no ++UE branch string in the first {scanned} bytes of {p.name}"}


def inspect_unreal(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    with open(p, "rb") as f:
        head = f.read(16)
    if head == UTOC_MAGIC:
        return {"format": "unreal_iostore", "kind": "utoc", "size": p.stat().st_size,
                "can_still_get": ["IoStore container presence (UE5 container table of contents)"],
                "blocker": "IoStore (.utoc/.ucas) extraction needs a parser such as CUE4Parse; not integrated"}
    foot = read_pak_footer(p)
    if foot is None:
        raise ValueError("no pak footer found")
    return {"format": "unreal_pak", "kind": "pak", "size": p.stat().st_size, "footer": foot,
            "can_still_get": ["pak version, index location and the encrypted-index flag"],
            "blocker": ("pak index is encrypted: contents need the game's AES key" if foot.get("encrypted_index") else
                        "no pak extractor or uasset parser is integrated; file names/contents are not listed")}


# =====================================================================================================================
# Unity IL2CPP global-metadata.dat
# =====================================================================================================================
IL2CPP_META_MAGIC = b"\xaf\x1b\xb1\xfa"


def inspect_il2cpp_metadata(path: Path | str, *, max_names: int = LIST_CAP) -> dict[str, Any]:
    """Read the identifier string section of global-metadata.dat (type, method, field, parameter names). No code exists here."""
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(32)
        if head[:4] != IL2CPP_META_MAGIC:
            raise ValueError("sanity header 0xFAB11BAF missing: metadata is absent, encrypted or obfuscated by the game")
        version = struct.unpack("<i", head[4:8])[0]
        s_off, s_size = struct.unpack("<II", head[24:32])
        out: dict[str, Any] = {"format": "il2cpp_metadata", "metadata_version": version, "size": size,
                               "identifier_section": {"offset": s_off, "size": s_size}}
        if not (16 <= version <= 40) or s_off + s_size > size or s_size == 0:
            out.update({"identifiers": [], "identifiers_truncated": False,
                        "note": "header offsets are inconsistent for this metadata version; identifiers not listed"})
        else:
            f.seek(s_off)
            blob = f.read(min(s_size, 8 * 1024 * 1024))
            names = [n.decode("utf-8", "replace") for n in blob.split(b"\0") if n]
            out["identifier_count_scanned"] = len(names)
            out["identifiers"] = names[:max_names]
            out["identifiers_truncated"] = len(names) > max_names or s_size > len(blob)
    out["can_still_get"] = ["metadata version", "identifier names (types, methods, fields, parameters)"]
    out["blocker"] = "method bodies are native code in GameAssembly; no IL2CPP dumper/lifter is integrated"
    return out


# =====================================================================================================================
# dispatch used by the triage backend
# =====================================================================================================================
def inspect_path(path: Path | str, profile: str | None = None) -> dict[str, Any]:
    """Pick the right inspector from the file itself (name + magic), falling back to ``profile``."""
    p = Path(path)
    with open(p, "rb") as f:
        head = f.read(32)
    name = p.name.lower()
    if macho_kind(head) is not None:
        return inspect_macho(p)
    if is_gamemaker_form(head):
        return inspect_gamemaker(p)
    if head[:4] == IL2CPP_META_MAGIC or name == "global-metadata.dat":
        return inspect_il2cpp_metadata(p)
    if name.endswith((".pak", ".utoc")):
        return inspect_unreal(p)
    raise ValueError(f"no inspector for {p.name} (profile {profile or 'unknown'})")


def unity_layout(names: list[str]) -> dict[str, Any]:
    """Evidence summary from relative file names of a Unity install (lower-case matching)."""
    low = [n.lower() for n in names]
    il2cpp_meta = [n for n in names if n.lower().endswith("il2cpp_data/metadata/global-metadata.dat")]
    managed = [n for n in names if re.search(r"(^|/)managed/assembly-csharp[^/]*\.dll$", n.lower())]
    ga = [n for n in names if os.path.basename(n.lower()) in ("gameassembly.dll", "gameassembly.so", "libil2cpp.so", "gameassembly.dylib")]
    player = [n for n in names if os.path.basename(n.lower()) in ("unityplayer.dll", "unityplayer.so", "unityplayer.dylib", "libunity.so")]
    appinfo = [n for n in names if n.lower().endswith("_data/app.info")]
    return {"il2cpp_metadata": il2cpp_meta, "managed_assemblies": managed, "game_assembly": ga, "unity_player": player, "app_info": appinfo,
            "data_dirs": sorted({n.split("/")[0] for n in names if "/" in n and n.split("/")[0].lower().endswith("_data")}),
            "has_unity_markers": bool(player or il2cpp_meta or managed or any(x.endswith("_data/globalgamemanagers") for x in low))}
