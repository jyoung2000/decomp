"""Pure-Python Android package inspection: binary AndroidManifest.xml (AXML), dex headers/class names and APK/AAB zip layout.

No external tool and no aapt. This is evidence, not recovery: it states what the package declares (package id, SDK levels,
permissions, components, launcher activity, dex/native-library inventory). Code recovery needs jadx (see jvm.py).

All reads are bounded: the manifest is capped, dex class listings are capped, and zip member sizes are checked before reading.
AAB manifests are protobuf (not AXML) and are reported as such rather than parsed.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from typing import Any

MAX_MANIFEST = 8 * 1024 * 1024
MAX_DEX_READ = 64 * 1024 * 1024
CLASS_CAP = 1000
ZIP_NAME_CAP = 200_000

RES_XML = 0x0003
RES_STRING_POOL = 0x0001
RES_XML_START_NS, RES_XML_END_NS, RES_XML_START_EL, RES_XML_END_EL, RES_XML_CDATA = 0x0100, 0x0101, 0x0102, 0x0103, 0x0104
RES_XML_RESOURCE_MAP = 0x0180

_ATTR_IDS = {  # android.R.attr resource ids -> readable attribute name (fallback when the string pool has no name)
    0x01010003: "name", 0x0101021B: "versionCode", 0x0101021C: "versionName", 0x0101020C: "minSdkVersion", 0x01010270: "targetSdkVersion",
    0x0101000F: "debuggable", 0x01010280: "allowBackup", 0x010104EC: "usesCleartextTraffic", 0x01010001: "label",
}


class AxmlError(ValueError):
    pass


def _read_string_pool(data: bytes, off: int) -> tuple[list[str], int]:
    ctype, hsize, size = struct.unpack_from("<HHI", data, off)
    if ctype != RES_STRING_POOL:
        raise AxmlError("string pool missing")
    count, style_count, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    if count > 200_000:
        raise AxmlError("implausible string count")
    offsets = struct.unpack_from(f"<{count}I", data, off + hsize)
    base = off + strings_start
    out: list[str] = []
    for so in offsets:
        p = base + so
        try:
            if utf8:
                n = data[p]
                p += 1
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | data[p]
                    p += 1
                bl = data[p]
                p += 1
                if bl & 0x80:
                    bl = ((bl & 0x7F) << 8) | data[p]
                    p += 1
                out.append(data[p:p + bl].decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", data, p)[0]
                p += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                    p += 2
                out.append(data[p:p + 2 * n].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out, off + size


def parse_axml(data: bytes) -> dict[str, Any]:
    """Parse a compiled binary XML document into {'tag','attrs','children'} (namespace prefixes dropped)."""
    if len(data) < 8:
        raise AxmlError("too short")
    ctype, hsize, size = struct.unpack_from("<HHI", data, 0)
    if ctype != RES_XML:
        raise AxmlError("not a compiled binary XML document (type 0x%04x)" % ctype)
    pos = hsize
    strings: list[str] = []
    res_map: list[int] = []
    root: dict[str, Any] | None = None
    stack: list[dict[str, Any]] = []
    end = min(size, len(data))
    while pos + 8 <= end:
        t, hs, sz = struct.unpack_from("<HHI", data, pos)
        if sz < 8 or pos + sz > len(data):
            raise AxmlError("chunk overruns the document")
        if t == RES_STRING_POOL:
            strings, _ = _read_string_pool(data, pos)
        elif t == RES_XML_RESOURCE_MAP:
            res_map = list(struct.unpack_from(f"<{(sz - hs) // 4}I", data, pos + hs))
        elif t == RES_XML_START_EL:
            b = pos + 16
            _ns, name_i, _astart, asize, acount = struct.unpack_from("<IIHHH", data, b)
            attrs: dict[str, Any] = {}
            ab = b + _astart
            for i in range(min(acount, 512)):
                a_ns, a_name, a_raw, _vs, _r0, a_type, a_data = struct.unpack_from("<IIIHBBI", data, ab + i * (asize or 20))
                nm = strings[a_name] if 0 <= a_name < len(strings) and strings[a_name] else _ATTR_IDS.get(res_map[a_name] if a_name < len(res_map) else -1, f"attr_{a_name}")
                if a_type == 0x03:
                    val: Any = strings[a_data] if a_data < len(strings) else None
                elif a_type == 0x12:
                    val = a_data != 0
                elif a_type in (0x10, 0x11):
                    val = a_data - (1 << 32) if a_data & 0x80000000 and a_type == 0x10 else a_data
                elif a_type == 0x01:
                    val = f"@0x{a_data:08x}"
                else:
                    val = strings[a_raw] if 0 <= a_raw < len(strings) else a_data
                attrs[nm] = val
            el = {"tag": strings[name_i] if name_i < len(strings) else f"tag_{name_i}", "attrs": attrs, "children": []}
            if stack:
                stack[-1]["children"].append(el)
            elif root is None:
                root = el
            stack.append(el)
        elif t == RES_XML_END_EL:
            if stack:
                stack.pop()
        pos += sz
    if root is None:
        raise AxmlError("no root element")
    return root


def _iter(el: dict[str, Any], tag: str):
    for c in el["children"]:
        if c["tag"] == tag:
            yield c


def summarize_manifest(root: dict[str, Any]) -> dict[str, Any]:
    a = root["attrs"]
    out: dict[str, Any] = {"package": a.get("package"), "version_code": a.get("versionCode"), "version_name": a.get("versionName"),
                           "compile_sdk": a.get("compileSdkVersion")}
    sdk = next(_iter(root, "uses-sdk"), None)
    out["min_sdk"] = sdk["attrs"].get("minSdkVersion") if sdk else None
    out["target_sdk"] = sdk["attrs"].get("targetSdkVersion") if sdk else None
    out["permissions"] = sorted({p["attrs"].get("name") for p in _iter(root, "uses-permission") if p["attrs"].get("name")})
    out["features"] = sorted({p["attrs"].get("name") for p in _iter(root, "uses-feature") if p["attrs"].get("name")})
    app = next(_iter(root, "application"), None)
    comps: dict[str, list[str]] = {"activity": [], "service": [], "receiver": [], "provider": []}
    launcher: list[str] = []
    if app:
        out["application_class"] = app["attrs"].get("name")
        out["debuggable"] = bool(app["attrs"].get("debuggable", False))
        out["allow_backup"] = app["attrs"].get("allowBackup")
        out["cleartext_traffic"] = app["attrs"].get("usesCleartextTraffic")
        for kind in comps:
            for c in _iter(app, kind):
                comps[kind].append(c["attrs"].get("name") or "")
                if kind == "activity":
                    for f in _iter(c, "intent-filter"):
                        acts = {x["attrs"].get("name") for x in _iter(f, "action")}
                        cats = {x["attrs"].get("name") for x in _iter(f, "category")}
                        if "android.intent.action.MAIN" in acts and "android.intent.category.LAUNCHER" in cats:
                            launcher.append(c["attrs"].get("name") or "")
    out["components"] = {k: v[:LIST_LIMIT] for k, v in comps.items()}
    out["component_counts"] = {k: len(v) for k, v in comps.items()}
    out["launcher_activities"] = launcher
    return out


LIST_LIMIT = 500


# ------------------------------------------------------------------------------------------------------------------ dex
def _uleb(data: bytes, p: int) -> tuple[int, int]:
    r = s = 0
    while True:
        b = data[p]
        p += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, p
        s += 7


def dex_header(data: bytes) -> dict[str, Any]:
    if len(data) < 0x70 or data[:4] != b"dex\n" or data[7:8] != b"\0":
        raise ValueError("not a dex file")
    (_ck, ) = struct.unpack_from("<I", data, 8)
    file_size, hdr, endian = struct.unpack_from("<III", data, 32)
    s_n, s_o, t_n, t_o, p_n, p_o, f_n, f_o, m_n, m_o, c_n, c_o = struct.unpack_from("<12I", data, 56)
    return {"version": data[4:7].decode("ascii", "replace"), "file_size": file_size, "header_size": hdr, "little_endian": endian == 0x12345678,
            "string_ids": s_n, "type_ids": t_n, "proto_ids": p_n, "field_ids": f_n, "method_ids": m_n, "class_defs": c_n,
            "_offsets": {"string": s_o, "type": t_o, "class": c_o}}


def dex_classes(data: bytes, *, cap: int = CLASS_CAP) -> tuple[list[str], bool]:
    h = dex_header(data)
    o = h.pop("_offsets")
    names: list[str] = []
    n = h["class_defs"]
    for i in range(min(n, cap)):
        class_idx = struct.unpack_from("<I", data, o["class"] + 32 * i)[0]
        desc_idx = struct.unpack_from("<I", data, o["type"] + 4 * class_idx)[0]
        soff = struct.unpack_from("<I", data, o["string"] + 4 * desc_idx)[0]
        _len, p = _uleb(data, soff)
        e = data.index(b"\0", p)
        d = data[p:e].decode("utf-8", "replace")
        names.append(d[1:-1].replace("/", ".") if d.startswith("L") and d.endswith(";") else d)
    return names, n > cap


# ------------------------------------------------------------------------------------------------------------------ zip
_FRAMEWORKS = [("libflutter.so", "flutter"), ("libunity.so", "unity"), ("libil2cpp.so", "unity-il2cpp"), ("libhermes.so", "react-native"),
               ("libreactnativejni.so", "react-native"), ("libmonodroid.so", "xamarin"), ("libcocos2dcpp.so", "cocos2d"),
               ("libgodot_android.so", "godot")]


def inspect_android_package(path: Path | str, *, include_classes: bool = True) -> dict[str, Any]:
    """Facts about an .apk/.aab: layout, manifest (APK only), dex inventory, native libs, frameworks, signing files."""
    p = Path(path)
    try:
        zf = zipfile.ZipFile(p)
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid zip: {e}") from e
    with zf:
        infos = zf.infolist()
        truncated = len(infos) > ZIP_NAME_CAP
        infos = infos[:ZIP_NAME_CAP]
        names = [i.filename for i in infos]
        by_name = {i.filename: i for i in infos}
        is_aab = "BundleConfig.pb" in by_name or "base/manifest/AndroidManifest.xml" in by_name
        manifest_name = "base/manifest/AndroidManifest.xml" if is_aab else "AndroidManifest.xml"
        dex_names = sorted(n for n in names if (n.startswith("base/dex/") if is_aab else "/" not in n) and n.endswith(".dex") and "classes" in n)
        out: dict[str, Any] = {"format": "aab" if is_aab else "apk", "entry_count": len(infos), "entries_truncated": truncated,
                               "manifest_present": manifest_name in by_name, "dex_files": [], "warnings": []}
        libs: dict[str, list[str]] = {}
        for n in names:
            if "/lib/" in n or n.startswith("lib/"):
                parts = n.split("/")
                if len(parts) >= 3 and parts[-1].endswith(".so"):
                    libs.setdefault(parts[-2], []).append(parts[-1])
        out["native_libraries"] = {abi: sorted(v)[:200] for abi, v in sorted(libs.items())}
        low = {n.rsplit("/", 1)[-1] for names_ in libs.values() for n in names_}
        fw = sorted({label for fn, label in _FRAMEWORKS if fn in low})
        if any(n.endswith("index.android.bundle") for n in names) and "react-native" not in fw:
            fw.append("react-native")
        if any(n.startswith("assemblies/") or "/assemblies/" in n for n in names) and "xamarin" not in fw:
            fw.append("xamarin")
        if any(n.startswith("assets/bin/Data/Managed/Metadata/") or n.endswith("global-metadata.dat") for n in names) and "unity-il2cpp" not in fw:
            fw.append("unity-il2cpp")
        out["frameworks"] = sorted(fw)
        out["signing_files"] = sorted(n for n in names if n.startswith("META-INF/") and n.rsplit(".", 1)[-1] in ("RSA", "DSA", "EC", "SF"))[:20]
        out["has_resources_arsc"] = "resources.arsc" in by_name or "base/resources.pb" in by_name
        out["res_file_count"] = sum(1 for n in names if n.startswith(("res/", "base/res/")))
        if manifest_name in by_name:
            info = by_name[manifest_name]
            if info.file_size > MAX_MANIFEST:
                out["warnings"].append("manifest larger than the safety cap; not parsed")
            elif is_aab:
                out["manifest"] = None
                out["warnings"].append("AAB manifests are protobuf-encoded; not parsed by the built-in AXML reader")
            else:
                try:
                    out["manifest"] = summarize_manifest(parse_axml(zf.read(info)))
                except (AxmlError, struct.error, IndexError) as e:
                    out["manifest"] = None
                    out["warnings"].append(f"manifest could not be parsed: {type(e).__name__}: {e}")
        total_classes = 0
        class_names: list[str] = []
        classes_truncated = False
        for dn in dex_names:
            info = by_name[dn]
            rec: dict[str, Any] = {"name": dn, "size": info.file_size}
            try:
                if info.file_size > MAX_DEX_READ:
                    with zf.open(info) as fh:
                        rec.update({k: v for k, v in dex_header(fh.read(0x70)).items() if k != "_offsets"})
                    rec["note"] = "larger than the read cap: header only, classes not listed"
                else:
                    data = zf.read(info)
                    h = dex_header(data)
                    h.pop("_offsets")
                    rec.update(h)
                    if include_classes and len(class_names) < CLASS_CAP:
                        names_, tr = dex_classes(data, cap=CLASS_CAP - len(class_names))
                        class_names += names_
                        classes_truncated = classes_truncated or tr
                total_classes += rec.get("class_defs", 0)
            except (ValueError, struct.error, IndexError) as e:
                rec["error"] = f"{type(e).__name__}: {e}"
            out["dex_files"].append(rec)
        out["dex_count"] = len(dex_names)
        out["class_count"] = total_classes
        out["multidex"] = len(dex_names) > 1
        if include_classes:
            out["classes"] = {"items": class_names, "total": total_classes, "truncated": classes_truncated or total_classes > len(class_names)}
        if not dex_names:
            out["warnings"].append("no classes*.dex: app code may be native/script based or the file is not a complete package")
    return out
