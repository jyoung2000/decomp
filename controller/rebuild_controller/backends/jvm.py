"""JVM / Android backend: CFR for .jar/.class, jadx for APK/DEX/AAB, plus pure-Python inspection that needs no tool.

What this backend genuinely does
- ``inspect``: reads jar/class/apk/aab/dex facts without any external tool (manifest, main class, class-file versions, multi-release,
  Spring Boot/WAR layout, obfuscation heuristic; for Android: binary AndroidManifest.xml via a small AXML parser, dex counts and class
  names, native libraries, framework hints). Works on a host with no Java at all.
- ``decompile``: ``.jar``/``.class`` run ``java -jar cfr.jar <input> --outputdir <out>``; ``.apk``/``.dex``/``.aab`` run jadx
  (``java -cp jadx-*-all.jar jadx.cli.JadxCLI``). Output is scanned for per-file decompiler failure/warning markers; a recovery report
  gives per-class status for jars (every top-level class is mapped to its expected ``.java`` file) and truncation/timeout disclosure.
  The recovered Java is a reconstruction: it is never claimed to compile or to be equivalent to the original source.
- Without jadx the Android path stops at ``inspect`` and says so: ``code recovery needs jadx`` (and a Java runtime).

Tools are run as separate processes only (CFR is MIT, jadx Apache-2.0, Temurin is GPLv2+Classpath); nothing is linked.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Settings, get_settings
from ..ids import sha256_file
from ..jobs.runner import StageError
from ..paths import PathPolicyError, assert_output_not_in_source, is_within, resolve_final
from . import android_info
from .archive import (RecoveryToolProbe, cap_list, discover_executable, failed_probe_info, probe_error, record_evidence, resolve_studio,
                      run_bounded, sha256_of)

BACKEND_ID = "jvm"
SCHEMA_VERSION = 1
CFR_VERSION = "0.152"
CFR_SHA256 = "f686e8f3ded377d7bc87d216a90e9e9512df4156e75b06c655a16648ae8765b2"
CFR_URL = "https://github.com/leibnitz27/cfr/releases/download/0.152/cfr-0.152.jar"
JADX_VERSION = "1.5.6"
JADX_SHA256 = "545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974"      # of jadx-1.5.6.zip
JADX_JAR_SHA256 = "fe3e12c45acf75f92369685fd02d1d7a7323385dc725680a9b98a0dac0ea554b"  # of lib/jadx-1.5.6-all.jar
JRE_VERSION = "17.0.20.1"
LIST_CAP = 2000
CLASS_SCAN_CAP = 50_000
JAR_INSTALL_HINT = ("Install the optional tools from docs/dependency-lock.json (Setup-Dependencies.ps1 -Tool temurin-jre,cfr,jadx, or the "
                    "Tools page in the app); or put cfr-0.152.jar in <tools>/cfr/ and any Java 8+ runtime on PATH")

_SMOKE_CLASS_B64 = (
    "yv66vgAAADQADAoAAgADBwAEDAAFAAYBABBqYXZhL2xhbmcvT2JqZWN0AQAGPGluaXQ+AQADKClW"
    "BwAIAQAKU21va2VQcm9iZQEABENvZGUBAAZhbnN3ZXIBAAMoKUkAMQAHAAIAAAAAAAIAAQAFAAYA"
    "AQAJAAAAEQABAAEAAAAFKrcAAbEAAAAAAAkACgALAAEACQAAAA8AAQAAAAAAAxAqrAAAAAAAAA=="
)

_CFR_FAILED_RE = re.compile(r"This method has failed to decompile|Decompilation failed")
_CFR_WARN_RE = re.compile(r"Unable to fully structure code|WARNING - |Could not load the following classes|\*\* Could not")
_JADX_ERR_RE = re.compile(r"JADX ERROR|Code restructure failed|Method dump skipped|Couldn't be decompiled")
_JADX_WARN_RE = re.compile(r"JADX WARN")
_MAJOR_TO_JAVA = lambda m: m - 44 if m >= 45 else None  # noqa: E731


# =====================================================================================================================
# class file / jar inspection (no tools)
# =====================================================================================================================
def read_class_header(data: bytes) -> dict[str, Any]:
    """Class file version plus this/super class names from the constant pool (bounded)."""
    if data[:4] != b"\xca\xfe\xba\xbe" or len(data) < 10:
        raise ValueError("not a class file")
    minor, major, cp_count = struct.unpack(">HHH", data[4:10])
    pos = 10
    utf8: dict[int, str] = {}
    cls: dict[int, int] = {}
    i = 1
    while i < cp_count:
        tag = data[pos]
        pos += 1
        if tag == 1:
            n = struct.unpack(">H", data[pos:pos + 2])[0]
            utf8[i] = data[pos + 2:pos + 2 + n].decode("utf-8", "replace")
            pos += 2 + n
        elif tag == 7:
            cls[i] = struct.unpack(">H", data[pos:pos + 2])[0]
            pos += 2
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            pos += 4
        elif tag in (5, 6):
            pos += 8
            i += 1
        elif tag in (8, 16, 19, 20):
            pos += 2
        elif tag == 15:
            pos += 3
        else:
            raise ValueError(f"unknown constant pool tag {tag}")
        i += 1
    access, this_i, super_i = struct.unpack(">HHH", data[pos:pos + 6])
    name = utf8.get(cls.get(this_i, -1), "").replace("/", ".")
    sup = utf8.get(cls.get(super_i, -1), "").replace("/", ".") if super_i else None
    return {"major": major, "minor": minor, "java_version": _MAJOR_TO_JAVA(major), "class_name": name, "super_class": sup,
            "access_flags": access}


def parse_manifest_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    last = None
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if ln.startswith(" ") and last:
            out[last] += ln[1:]
        elif ": " in ln:
            last, v = ln.split(": ", 1)
            out[last] = v
        elif ln == "":
            if out:
                break   # main section only
    return out


def inspect_jar(path: Path | str, *, include_classes: bool = True) -> dict[str, Any]:
    p = Path(path)
    with zipfile.ZipFile(p) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        class_infos = [i for i in infos if i.filename.endswith(".class") and not i.is_dir()]
        manifest: dict[str, str] = {}
        mi = next((i for i in infos if i.filename.upper() == "META-INF/MANIFEST.MF"), None)
        if mi is not None and mi.file_size <= 1024 * 1024:
            manifest = parse_manifest_text(zf.read(mi).decode("utf-8", "replace"))
        versions: dict[int, int] = {}
        scanned = 0
        simple_names: list[str] = []
        top: list[dict[str, Any]] = []
        for ci in class_infos[:CLASS_SCAN_CAP]:
            scanned += 1
            with zf.open(ci) as fh:
                head = fh.read(8)
            major = struct.unpack(">H", head[6:8])[0] if len(head) == 8 and head[:4] == b"\xca\xfe\xba\xbe" else None
            if major:
                versions[major] = versions.get(major, 0) + 1
            n = ci.filename[:-6]
            base = n.rsplit("/", 1)[-1]
            versioned = n.startswith("META-INF/versions/")
            if not versioned and "$" not in base and base not in ("module-info", "package-info"):
                top.append({"name": n.replace("/", "."), "path": ci.filename, "major": major, "size": ci.file_size})
                simple_names.append(base)
        single = sum(1 for s in simple_names if len(s) <= 2)
        flags: dict[str, Any] = {
            "multi_release": manifest.get("Multi-Release", "").lower() == "true",
            "spring_boot": any(n.startswith("BOOT-INF/classes/") for n in names),
            "war": any(n.startswith("WEB-INF/classes/") for n in names),
            "signed": any(n.startswith("META-INF/") and n.rsplit(".", 1)[-1] in ("SF", "RSA", "DSA", "EC") for n in names),
            "kotlin": any(n.endswith(".kotlin_module") for n in names) or any(n.startswith("kotlin/") for n in names),
            "scala": any(n.startswith("scala/") for n in names),
            "agent": "Premain-Class" in manifest or "Agent-Class" in manifest,
            "likely_obfuscated": len(simple_names) >= 20 and single / len(simple_names) >= 0.5,
            "nested_jars": sum(1 for n in names if n.endswith(".jar")),
        }
        total = sum(i.file_size for i in infos)
        out = {"format": "jar", "entry_count": len(infos), "uncompressed_bytes": total, "class_count": len(class_infos),
               "classes_scanned": scanned, "classes_scan_truncated": len(class_infos) > CLASS_SCAN_CAP,
               "top_level_class_count": len(top), "main_class": manifest.get("Main-Class"), "manifest": manifest,
               "class_file_versions": {str(k): v for k, v in sorted(versions.items())},
               "java_versions": sorted({_MAJOR_TO_JAVA(k) for k in versions if _MAJOR_TO_JAVA(k)}), "flags": flags,
               "versioned_class_count": sum(1 for i in class_infos if i.filename.startswith("META-INF/versions/")),
               "resource_count": sum(1 for n in names if not n.endswith(("/", ".class")))}
        if include_classes:
            out["classes"] = cap_list(top, LIST_CAP)
        out["_top"] = top
    return out


def inspect_any(path: Path | str, *, include_classes: bool = True) -> dict[str, Any]:
    """Dispatch by content: class file, dex, zip (apk/aab/jar)."""
    p = Path(path)
    with open(p, "rb") as f:
        head = f.read(8)
    if head[:4] == b"\xca\xfe\xba\xbe":
        data = p.read_bytes()[: 8 * 1024 * 1024]
        return {"format": "class", **read_class_header(data), "size": p.stat().st_size}
    if head[:4] == b"dex\n":
        data = p.read_bytes()[: android_info.MAX_DEX_READ]
        h = android_info.dex_header(data)
        h.pop("_offsets")
        out = {"format": "dex", **h, "size": p.stat().st_size}
        if include_classes:
            names, tr = android_info.dex_classes(data)
            out["classes"] = {"items": names, "total": h["class_defs"], "truncated": tr}
        return out
    if head[:2] == b"PK":
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
        if "AndroidManifest.xml" in names or "BundleConfig.pb" in names or "base/manifest/AndroidManifest.xml" in names:
            return android_info.inspect_android_package(p, include_classes=include_classes)
        return inspect_jar(p, include_classes=include_classes)
    raise ValueError(f"{p.name} is not a class, dex or zip-based package")


# =====================================================================================================================
# output scanning
# =====================================================================================================================
def _expected_java_path(class_name: str) -> str:
    return class_name.replace(".", "/") + ".java"


def scan_java_output(out: Path, *, engine: str, cap: int) -> dict[str, Any]:
    """Hash and marker-scan every .java under ``out`` (bounded by ``cap`` files)."""
    files: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    totals = {"failed_markers": 0, "warning_markers": 0}
    per_file: dict[str, tuple[int, int]] = {}
    scanned = 0
    truncated = False
    fail_re, warn_re = (_CFR_FAILED_RE, _CFR_WARN_RE) if engine == "cfr" else (_JADX_ERR_RE, _JADX_WARN_RE)
    for fp in sorted(out.rglob("*")):
        if not fp.is_file() or fp.is_symlink():
            continue
        scanned += 1
        if scanned > cap:
            truncated = True
            break
        rel = fp.relative_to(out).as_posix()
        data = fp.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        digest.update(f"{rel}\0{h}\n".encode())
        entry: dict[str, Any] = {"path": rel, "bytes": len(data), "sha256": h}
        if fp.suffix == ".java":
            text = data.decode("utf-8", "replace")
            fm, wm = len(fail_re.findall(text)), len(warn_re.findall(text))
            entry.update({"lines": text.count("\n") + 1, "failed_markers": fm, "warning_markers": wm})
            totals["failed_markers"] += fm
            totals["warning_markers"] += wm
            per_file[rel] = (fm, wm)
        files.append(entry)
    return {"files": files, "tree_sha256": digest.hexdigest(), "totals": totals, "per_file": per_file, "scan_truncated": truncated,
            "java_files": sum(1 for f in files if f["path"].endswith(".java"))}


# =====================================================================================================================
# backend
# =====================================================================================================================
_PROBE_CACHE: dict[tuple[str, float], tuple[str | None, str]] = {}


def _run_quick(settings: Settings, cmd: list[str], timeout: float = 60) -> tuple[int, str, str]:
    r = run_bounded(None, cmd, limits=settings.limits, timeout=timeout)
    return r.returncode, r.text, r.err_text


class JVMBackend(BackendAdapter):
    backend_id = BACKEND_ID

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # -- discovery -------------------------------------------------------------------------------------------------
    def find_java(self) -> Path | None:
        extra = []
        jh = os.environ.get("JAVA_HOME")
        if jh:
            extra.append(Path(jh) / "bin")
        return discover_executable(self.settings, ["java"], subdirs=["jre", "jdk", "java"], extra_dirs=extra, bin_subpaths=("bin",))

    def _tool_dirs(self, sub: str) -> list[Path]:
        dirs = [Path(self.settings.tools_dir) / sub]
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            dirs.append(Path(os.environ["LOCALAPPDATA"]) / "RebuildStudio" / "tools" / sub)
        return dirs

    def find_cfr(self) -> Path | None:
        env = os.environ.get("REBUILD_CFR_JAR")
        if env and Path(env).is_file():
            return Path(env)
        for d in self._tool_dirs("cfr"):
            jars = sorted(d.glob("cfr*.jar")) if d.is_dir() else []
            if jars:
                return jars[-1]
        return None

    def find_jadx(self) -> Path | None:
        env = os.environ.get("REBUILD_JADX_JAR")
        if env and Path(env).is_file():
            return Path(env)
        for d in self._tool_dirs("jadx"):
            jars = sorted((d / "lib").glob("jadx-*-all.jar")) if (d / "lib").is_dir() else []
            if jars:
                return jars[-1]
        return None

    # -- probing ---------------------------------------------------------------------------------------------------
    def _java_probe(self, java: Path | None) -> RecoveryToolProbe:
        if java is None:
            return RecoveryToolProbe("java", Availability.MISSING, detail="no Java runtime in tools dir, JAVA_HOME or PATH", license="GPL-2.0-with-classpath-exception",
                                     source="https://adoptium.net/temurin/", pinned=f"Temurin {JRE_VERSION}+1 JRE (docs/dependency-lock.json: temurin-jre)",
                                     next_action=JAR_INSTALL_HINT, prerequisites=[])
        key = (str(java), _mtime(java))
        if key not in _PROBE_CACHE:
            ver, detail = None, ""
            try:
                rc, out, err = _run_quick(self.settings, [str(java), "-version"], 30)
                m = re.search(r'version "([^"]+)"', err + out)
                if rc == 0 and m:
                    ver = m.group(1)
                else:
                    detail = (err or out)[:300] or f"exit {rc}"
            except (StageError, OSError) as e:
                detail = f"{type(e).__name__}: {e}"
            _PROBE_CACHE[key] = (ver, detail)
        ver, detail = _PROBE_CACHE[key]
        if ver is None:
            return RecoveryToolProbe("java", Availability.DETECTED, path=str(java), detail=f"found but -version failed: {detail}", next_action=JAR_INSTALL_HINT,
                                     license="GPL-2.0-with-classpath-exception", source="https://adoptium.net/temurin/")
        return RecoveryToolProbe("java", Availability.INSTALLED, path=str(java), version=ver, license="GPL-2.0-with-classpath-exception",
                                 source="https://adoptium.net/temurin/", pinned=f"{JRE_VERSION}+1", detail="" if ver.startswith(JRE_VERSION) else
                                 f"not the pinned Temurin {JRE_VERSION}; any Java 11+ works for CFR and jadx",
                                 integrity=sha256_of(java) if java.stat().st_size < 64 * 1024 * 1024 else "")

    def _jar_probe(self, name: str, jar: Path | None, java: ToolProbe, *, version_cmd: list[str], pinned: str, sha: str, license: str,
                   source: str, optional: bool, hint: str) -> RecoveryToolProbe:
        if jar is None:
            return RecoveryToolProbe(name, Availability.MISSING, detail=f"{name} jar not found in the tools dir", license=license, source=source,
                                     pinned=pinned, next_action=hint, optional=optional, prerequisites=["Java runtime"])
        if java.availability not in (Availability.INSTALLED, Availability.USABLE, Availability.VERIFIED) or not java.path:
            return RecoveryToolProbe(name, Availability.DETECTED, path=str(jar), detail="found but no working Java runtime to run it", license=license,
                                     source=source, pinned=pinned, next_action=JAR_INSTALL_HINT, optional=optional, prerequisites=["Java runtime"])
        key = (str(jar), _mtime(jar))
        if key not in _PROBE_CACHE:
            ver, detail = None, ""
            try:
                rc, out, err = _run_quick(self.settings, [java.path, *version_cmd], 60)
                m = re.search(r"(\d+\.\d+(?:\.\d+)*)", out + err)
                if rc == 0 and m:
                    ver = m.group(1)
                else:
                    detail = (err or out)[:300] or f"exit {rc}"
            except (StageError, OSError) as e:
                detail = f"{type(e).__name__}: {e}"
            _PROBE_CACHE[key] = (ver, detail)
        ver, detail = _PROBE_CACHE[key]
        if ver is None:
            return RecoveryToolProbe(name, Availability.DETECTED, path=str(jar), detail=f"found but version check failed: {detail}", license=license,
                                     source=source, pinned=pinned, optional=optional, next_action=hint, prerequisites=["Java runtime"])
        digest = sha256_of(jar)
        notes = []
        if ver != pinned:
            notes.append(f"version differs from pinned {pinned}")
        if digest != sha:
            notes.append("sha256 differs from the pinned artifact")
        return RecoveryToolProbe(name, Availability.INSTALLED, path=str(jar), version=ver, license=license, source=source, pinned=pinned, integrity=digest,
                                 detail="; ".join(notes), optional=optional, prerequisites=["Java runtime"], next_action=hint if notes else "")

    def probe(self) -> BackendInfo:
        try:
            return self._probe()
        except Exception as e:  # noqa: BLE001 - probe() must never raise
            return failed_probe_info(BACKEND_ID, "Java / Android recovery (CFR, jadx)", "cfr", e, JAR_INSTALL_HINT)

    def _probe(self) -> BackendInfo:
        java = self._java_probe(self.find_java())
        cfr = self._jar_probe("cfr", self.find_cfr(), java, version_cmd=["-jar", str(self.find_cfr() or ""), "--version"], pinned=CFR_VERSION,
                              sha=CFR_SHA256, license="MIT", source="https://github.com/leibnitz27/cfr", optional=False, hint=JAR_INSTALL_HINT)
        jj = self.find_jadx()
        jadx = self._jar_probe("jadx", jj, java, version_cmd=["-cp", str(jj or ""), "jadx.cli.JadxCLI", "--version"], pinned=JADX_VERSION,
                               sha=JADX_JAR_SHA256, license="Apache-2.0", source="https://github.com/skylot/jadx", optional=True,
                               hint="Optional (Android code recovery only): Setup-Dependencies.ps1 -Tool jadx")
        return BackendInfo(
            backend_id=BACKEND_ID, title="Java / Android recovery (CFR, jadx)", formats=["jar", "java_class", "apk", "aab", "dex"],
            platforms=["windows", "linux", "macos"], profiles=["jvm", "android"],
            operations=[
                Operation("detect", "Classify jar/class/apk/aab/dex and state what can be recovered", {"path": "path"}, {"support": "dict"}),
                Operation("inspect", "Tool-free facts: manifest, main class, class versions, Android manifest, dex inventory", {"module_path": "path"}, {"inspect": "dict"}),
                Operation("decompile", "Decompile a jar/class with CFR or an apk/dex/aab with jadx; per-class recovery report",
                          {"module_path": "path", "out_dir": "path"}, {"recovery_report": "dict"}),
            ],
            tools=[java, cfr, jadx],
            resources={"typical_seconds_per_jar": "1-120", "ram_mb": 512, "android": "code recovery needs jadx (optional tool); inspect always works",
                       "next_action": (java.next_action or cfr.next_action) if cfr.availability not in (Availability.INSTALLED, Availability.USABLE) else ""},
            experimental=False)

    def smoke(self) -> ToolProbe:
        info = self.probe()
        cfr = next(t for t in info.tools if t.name == "cfr")
        java = next(t for t in info.tools if t.name == "java")
        if cfr.availability not in (Availability.INSTALLED, Availability.USABLE) or not java.path or not cfr.path:
            return cfr
        with tempfile.TemporaryDirectory(prefix="rs-jvm-smoke-") as td:
            cls = Path(td) / "SmokeProbe.class"
            cls.write_bytes(base64.b64decode(_SMOKE_CLASS_B64))
            try:
                r = run_bounded(None, [java.path, "-Dfile.encoding=UTF-8", "-jar", cfr.path, str(cls), "--silent", "true"], limits=self.settings.limits, timeout=120)
            except (StageError, OSError) as e:
                cfr.detail = f"smoke failed: {e}"
                return cfr
            if r.returncode == 0 and "public static int answer()" in r.text and "return 42;" in r.text:
                cfr.availability = Availability.USABLE
                cfr.extra = {"proves": ["java"]}
                cfr.detail = "decompiled the built-in sample class (SmokeProbe.answer) with the discovered Java runtime"
            else:
                cfr.detail = f"smoke failed: exit {r.returncode}: {(r.err_text or r.text)[:300]}"
        return cfr

    # -- operations ------------------------------------------------------------------------------------------------
    def op_detect(self, ctx: Any, path: str, **kw: Any) -> OperationResult:
        return self.detect(path)

    def op_inspect(self, ctx: Any, module_path: str, **kw: Any) -> OperationResult:
        return self.inspect(module_path, ctx=ctx, **kw)

    def op_decompile(self, ctx: Any, module_path: str, out_dir: str, **kw: Any) -> OperationResult:
        return self.decompile(module_path, out_dir, ctx=ctx, **kw)

    def detect(self, path: Path | str, **_: Any) -> OperationResult:
        from .detect import detect_path
        return OperationResult(ok=True, data=detect_path(path))

    def inspect(self, module_path: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
                module_id: str | None = None, **_: Any) -> OperationResult:
        mp = Path(module_path)
        if not mp.is_file():
            return OperationResult(ok=False, error=f"module not found: {mp}")
        try:
            info = inspect_any(mp)
        except (ValueError, zipfile.BadZipFile, struct.error, OSError) as e:
            return OperationResult(ok=False, error=f"cannot inspect {mp.name}: {type(e).__name__}: {e}")
        info.pop("_top", None)
        sha = sha256_file(mp)
        profile = "android" if info["format"] in ("apk", "aab", "dex") else "jvm"
        from .support import support_for
        body = {"schema": SCHEMA_VERSION, "module": {"path": str(mp), "sha256": sha, "size": mp.stat().st_size}, "profile": profile,
                "inspect": info, "support": support_for(profile, {"aab": info["format"] == "aab", "frameworks": info.get("frameworks"),
                                                                    "spring_boot": (info.get("flags") or {}).get("spring_boot")}),
                "tool": {"name": "builtin-inspector", "version": str(SCHEMA_VERSION)}}
        inputs = {"op": "inspect", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": "builtin-inspector", "tool_version": str(SCHEMA_VERSION),
                  "module_sha256": sha}
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "jvm.inspect", f"JVM/Android facts: {mp.name}", body, module_id=module_id,
                              inputs=inputs, producer=BACKEND_ID)
        trunc = bool((info.get("classes") or {}).get("truncated"))
        return OperationResult(ok=True, data=body, evidence_ids=[eid] if eid else [], truncated=trunc)

    def decompile(self, module_path: Path | str, out_dir: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
                  module_id: str | None = None, source_root: Path | str | None = None, timeout: float = 600, **_: Any) -> OperationResult:
        mp, out = Path(module_path), Path(out_dir)
        if not mp.is_file():
            return OperationResult(ok=False, error=f"module not found: {mp}")
        try:
            if source_root is not None:
                assert_output_not_in_source(out, Path(source_root))
            if is_within(resolve_final(mp), resolve_final(out)):
                raise PathPolicyError(f"output directory {out} contains the module being decompiled")
        except PathPolicyError as e:
            return OperationResult(ok=False, error=f"path policy: {e}")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            return OperationResult(ok=False, error=f"output directory {out} exists and is not empty; refusing to mix outputs")
        studio_obj = resolve_studio(ctx, studio)
        sha = sha256_file(mp)
        try:
            info = inspect_any(mp, include_classes=False)
        except (ValueError, zipfile.BadZipFile, struct.error, OSError) as e:
            return OperationResult(ok=False, error=f"{mp.name} is not a recoverable JVM/Android module: {type(e).__name__}: {e}")
        fmt = info["format"]
        lim = self.settings.limits
        if fmt == "jar" and (info["entry_count"] > lim.max_archive_entries or info["uncompressed_bytes"] > lim.max_archive_expansion_bytes):
            return OperationResult(ok=False, error="archive exceeds the configured entry/expansion limits; refusing to decompile "
                                   f"({info['entry_count']} entries, {info['uncompressed_bytes']} bytes)")
        engine = "cfr" if fmt in ("jar", "class") else "jadx"
        tools = {t.name: t for t in self.probe().tools}
        java, tool = tools["java"], tools[engine]
        inputs_base = {"op": "decompile", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": engine, "tool_version": tool.version,
                       "java_version": java.version, "module_sha256": sha}
        if tool.availability not in (Availability.INSTALLED, Availability.USABLE, Availability.VERIFIED) or not java.path:
            what = "jadx" if engine == "jadx" else "CFR"
            blocker = (f"code recovery needs {what}" + ("" if java.path else " and a Java runtime") + f" ({tool.availability.value}: {tool.detail or 'not installed'})")
            body = {"schema": SCHEMA_VERSION, "status": "blocked", "engine": engine, "module": {"path": str(mp), "sha256": sha}, "format": fmt,
                    "blocker": blocker, "next_action": tool.next_action or java.next_action or JAR_INSTALL_HINT,
                    "inspect_available": True, "recovered": False, "equivalence_claimed": False}
            eid = record_evidence(studio_obj, case_id, "jvm.recovery_report", f"Recovery blocked ({engine}): {mp.name}", body, module_id=module_id,
                                  inputs=inputs_base, producer=BACKEND_ID)
            return OperationResult(ok=False, error=blocker + f". next_action: {body['next_action']}", data=body, evidence_ids=[eid] if eid else [])
        out.mkdir(parents=True, exist_ok=True)
        if engine == "cfr":
            cmd = [java.path, "-Dfile.encoding=UTF-8", "-Djava.awt.headless=true", "-jar", tool.path, str(mp), "--outputdir", str(out),
                   "--outputencoding", "UTF-8", "--silent", "true"]
            cmd_text = "java -jar cfr.jar <module> --outputdir <out> --outputencoding UTF-8 --silent true"
        else:
            cmd = [java.path, "-Dfile.encoding=UTF-8", "-Djava.awt.headless=true", "-cp", tool.path, "jadx.cli.JadxCLI", "-d", str(out), "--log-level", "WARN", str(mp)]
            cmd_text = "java -cp jadx-all.jar jadx.cli.JadxCLI -d <out> --log-level WARN <module>"
        timed_out = False
        run_msg = ""
        started = time.time()
        try:
            r = run_bounded(ctx, cmd, limits=lim, timeout=timeout)
            rc, out_text, err_text, out_trunc = r.returncode, r.text, r.err_text, r.truncated
        except StageError as e:
            timed_out, rc, out_text, err_text, out_trunc, run_msg = True, -1, "", str(e), False, str(e)
        scan = scan_java_output(out, engine=engine, cap=lim.max_inventory_files)
        totals = scan["totals"]
        tool_log = ""
        slog = out / "summary.txt"
        if engine == "cfr" and slog.is_file():
            tool_log = slog.read_text("utf-8", "replace")[:4000]
        per_class: list[dict[str, Any]] = []
        counts = {"decompiled": 0, "decompiled_with_warnings": 0, "failed_methods": 0, "failed": 0, "not_attempted": 0}
        if engine == "cfr" and fmt == "jar":
            full = inspect_jar(mp)
            top = full["_top"]
            for t in top:
                rel = _expected_java_path(t["name"])
                if rel in scan["per_file"]:
                    fm, wm = scan["per_file"][rel]
                    st = "failed_methods" if fm else ("decompiled_with_warnings" if wm else "decompiled")
                    counts[st] += 1
                    per_class.append({"class": t["name"], "status": st, "file": rel, "failed_markers": fm, "warning_markers": wm})
                else:
                    st = "not_attempted" if timed_out else "failed"
                    counts[st] += 1
                    per_class.append({"class": t["name"], "status": st, "file": None, "failed_markers": 0, "warning_markers": 0})
        n_expected = len(per_class)
        status = "ok"
        if timed_out or counts["failed"] or counts["not_attempted"] or counts["failed_methods"]:
            status = "partial"
        elif engine == "jadx" and rc != 0:
            status = "partial"
        elif totals["failed_markers"]:
            status = "partial"
        if scan["java_files"] == 0:
            status = "failed"
        report: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "status": status, "engine": engine, "format": fmt,
            "module": {"path": str(mp), "sha256": sha, "size": mp.stat().st_size, "main_class": info.get("main_class"),
                       "class_file_versions": info.get("class_file_versions"), "flags": info.get("flags") or None,
                       "frameworks": info.get("frameworks")},
            "tool": {"name": engine, "version": tool.version, "path_sha256": tool.integrity, "command": cmd_text},
            "java": {"version": java.version},
            "classes_total": n_expected if engine == "cfr" else info.get("class_count"),
            "classes_decompiled": counts["decompiled"], "classes_with_warnings": counts["decompiled_with_warnings"],
            "classes_failed": counts["failed"], "classes_with_failed_methods": counts["failed_methods"], "classes_not_attempted": counts["not_attempted"],
            "java_files": scan["java_files"], "failed_markers": totals["failed_markers"], "warning_markers": totals["warning_markers"],
            "exit_code": rc, "timed_out": timed_out, "duration_s": round(time.time() - started, 2),
            "stdout_truncated": out_trunc, "stderr_excerpt": err_text[:2000], "tool_log_excerpt": tool_log,
            "files": cap_list([f for f in scan["files"] if f["path"] != "summary.txt"], LIST_CAP),
            "output_scan_truncated": scan["scan_truncated"],
            "per_class": cap_list(per_class, LIST_CAP), "output_tree_sha256": scan["tree_sha256"],
            "output_file_count": len(scan["files"]),
            "claims": ("Per-class decompiler status only. The Java is a reconstruction: it is not asserted to compile or to be equivalent to the original source."
                       + (" Names of obfuscated classes/members are not recoverable." if (info.get("flags") or {}).get("likely_obfuscated") else "")),
            "equivalence_claimed": False,
        }
        if fmt == "jar" and (info.get("flags") or {}).get("nested_jars"):
            report["notes"] = [f"{info['flags']['nested_jars']} nested jar(s) inside the archive were not decompiled separately"]
        if engine == "jadx":
            res_dir = out / "resources"
            report["resources_decoded"] = {"dir_present": res_dir.is_dir(), "manifest_decoded": (res_dir / "AndroidManifest.xml").is_file(),
                                           "file_count": sum(1 for _ in res_dir.rglob("*") if _.is_file()) if res_dir.is_dir() else 0}
            report["android"] = {"dex_count": info.get("dex_count"), "class_count": info.get("class_count"),
                                 "note": "jadx renames obfuscated classes, so classes are not mapped one-to-one to output files"}
        if timed_out:
            report["timeout_message"] = run_msg
        eid = record_evidence(studio_obj, case_id, "jvm.recovery_report", f"{engine.upper()} recovery report: {mp.name}", report, module_id=module_id,
                              inputs=inputs_base, producer=BACKEND_ID)
        truncated = (report["files"]["truncated"] or report["per_class"]["truncated"] or scan["scan_truncated"] or timed_out or out_trunc
                     or counts["not_attempted"] > 0)
        ok = scan["java_files"] > 0
        return OperationResult(ok=ok, data={"recovery_report": report, "out_dir": str(out)}, evidence_ids=[eid] if eid else [], truncated=truncated,
                               error=None if ok else f"{engine} produced no Java sources (exit {rc}): {(err_text or out_text)[:300]}")


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0
