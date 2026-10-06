"""GDRE Tools (gdsdecomp) backend for Godot games.

What this backend genuinely does
- ``detect``: finds a Godot PCK (``GDPC`` magic at offset 0, an embedded trailer ``[pck][u64 size]GDPC``, or a PE/ELF
  section named ``pck``), parses the header natively (pack version, engine version, flags) and, for pack versions 0-2
  with an unencrypted directory, lists the entries without running GDRE.
- ``recover``: runs ``gdre_tools --headless --recover=<pck> --output=<dir>`` and turns the stdout/log into a recovery
  report. Resources come from GDRE's converters; GDScript bytecode (.gdc) is decompiled by GDRE; anything GDRE could not
  do is listed as an explicit gap (undecompiled scripts, failed/unconverted resources, missing GDExtension libraries,
  encryption, C# assemblies that live outside the PCK, ERROR lines). Nothing here claims behavioural equivalence.

Verified against GDRE tools 2.7.0 (Godot 4.3-format PCK, pack version 2). Pack version 3 headers are recognised but their
directory is not parsed natively (GDRE itself is still invoked). PCK versions 0/1 are parsed natively but have no GDRE
regression in this repo.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Settings, get_settings
from ..ids import sha256_file
from ..jobs.runner import StageError
from ..paths import PathPolicyError, assert_output_not_in_source, is_within, resolve_final
from .archive import (failed_probe_info, RecoveryToolProbe, cap_list, discover_executable, probe_error, record_evidence, resolve_studio,
                      run_bounded, sha256_of)

BACKEND_ID = "gdre"
TOOL_NAME = "gdre_tools"
PINNED_VERSION = "2.7.0"
PINNED_ZIP = "GDRE_tools-v2.7.0-linux.zip"
PINNED_ZIP_SHA256 = "abb4c197fe517d6a46b67faf41fc76d9890c882661a55ca2d49ba605955dfacc"
LICENSE = "MIT"
SOURCE_URL = "https://github.com/GDRETools/gdsdecomp"
INSTALL_HINT = ("Download GDRE tools 2.7.0 from https://github.com/GDRETools/gdsdecomp/releases/tag/v2.7.0, verify the zip "
                f"sha256 {PINNED_ZIP_SHA256}, and unpack it to <tools_dir>/gdre/")
SCHEMA_VERSION = 1
PCK_MAGIC = b"GDPC"
LIST_CAP = 2000
LOG_EXCERPT_BYTES = 64 * 1024
PACK_DIR_ENCRYPTED = 1
PACK_REL_FILEBASE = 2
SCRIPT_BYTECODE_EXT = (".gdc", ".gde")

_PROGRESS_RE = re.compile(r"\[[ =]*\]\s*\d+%")
_TOTAL_KEYS = {
    "Decompiled scripts": "scripts_decompiled", "Scripts not decompiled": "scripts_not_decompiled",
    "Imported resources for export session": "imported_resources", "Successfully converted": "converted",
    "Lossy": "lossy", "Rewrote metadata": "rewrote_metadata", "Non-importable conversions": "non_importable",
    "Not converted": "not_converted", "Failed conversions": "failed_conversions",
}


# =====================================================================================================================
# PCK parsing (native)
# =====================================================================================================================
def _pad4(n: int) -> int:
    return (n + 3) & ~3


def build_probe_pck(files: list[tuple[str, bytes]], version: tuple[int, int, int] = (4, 3, 0), *, embed_flags: int = 0) -> bytes:
    """Minimal Godot 4 PCK writer (pack version 2). Used by smoke(); the tests carry their own independent packer."""
    dsz = sum(4 + _pad4(len(p.encode()) + 1) + 8 + 8 + 16 + 4 for p, _ in files)
    base = (100 + dsz + 15) // 16 * 16
    ents, data = b"", b""
    for p, d in files:
        pb = p.encode() + b"\0"
        pb += b"\0" * (_pad4(len(pb)) - len(pb))
        ents += struct.pack("<I", len(pb)) + pb + struct.pack("<QQ", len(data), len(d)) + hashlib.md5(d).digest() + struct.pack("<I", 0)  # noqa: S324
        data += d + b"\0" * ((-len(d)) % 16)
    head = PCK_MAGIC + struct.pack("<IIIIIQ", 2, *version, embed_flags, base) + b"\0" * 64 + struct.pack("<I", len(files))
    blob = head + ents
    return blob + b"\0" * (base - len(blob)) + data


def _find_pck_start(path: Path, size: int) -> tuple[str, int, int | None, list[str]]:
    """Return (kind, pck_start, pck_size|None, notes). kind: pck | embedded_pck | none."""
    notes: list[str] = []
    with open(path, "rb") as f:
        head = f.read(4)
        if head == PCK_MAGIC:
            return "pck", 0, size, notes
        if size >= 12:
            f.seek(size - 12)
            tail = f.read(12)
            if tail[8:] == PCK_MAGIC:
                ds = struct.unpack_from("<Q", tail, 0)[0]
                start = size - 12 - ds
                if 0 <= start and ds >= 100:
                    f.seek(start)
                    if f.read(4) == PCK_MAGIC:
                        return "embedded_pck", start, ds, notes
                    notes.append("trailer GDPC found but no PCK header at the declared start")
    try:
        import lief
        b = lief.parse(str(path))
        for s in (b.sections if b is not None else []):
            if s.name.strip("\0") == "pck" and s.size >= 100:
                with open(path, "rb") as f:
                    f.seek(s.offset)
                    if f.read(4) == PCK_MAGIC:
                        return "embedded_pck", int(s.offset), int(s.size), notes + ["embedded in a 'pck' executable section"]
    except Exception as e:  # noqa: BLE001 - lief raises many types on non-executables
        notes.append(f"section scan skipped: {type(e).__name__}")
    return "none", 0, None, notes + ["no GDPC header, trailer or 'pck' section found"]


def read_pck_header(path: Path | str, *, max_entries: int = 200_000, list_entries: bool = True) -> dict[str, Any]:
    """Parse a PCK (standalone or embedded). Raises ValueError for malformed headers."""
    p = Path(path)
    size = p.stat().st_size
    kind, start, pck_size, notes = _find_pck_start(p, size)
    out: dict[str, Any] = {"kind": kind, "path": str(p), "size": size, "pck_start": start, "pck_size": pck_size,
                           "parse_notes": notes}
    if kind == "none":
        return out
    with open(p, "rb") as f:
        f.seek(start)
        fixed = f.read(20)
        if len(fixed) < 20:
            raise ValueError("truncated PCK header")
        ver, major, minor, patch = struct.unpack_from("<IIII", fixed, 4)
        out.update({"pack_version": ver, "engine_version": f"{major}.{minor}.{patch}", "engine_major": major,
                    "engine_minor": minor, "engine_patch": patch})
        if ver > 3:
            raise ValueError(f"unknown PCK format version {ver}")
        flags, file_base, dir_offset = 0, 0, None
        if ver >= 2:
            more = f.read(12)
            flags = struct.unpack_from("<I", more, 0)[0]
            file_base = struct.unpack_from("<Q", more, 4)[0]
            if ver == 3:
                dir_offset = struct.unpack_from("<Q", f.read(8), 0)[0]
        f.read(64)  # 16 reserved int32
        if ver == 3:
            out["parse_notes"] = notes + ["pack version 3: directory not parsed natively; rely on GDRE output"]
            out.update({"flags": {"dir_encrypted": bool(flags & PACK_DIR_ENCRYPTED), "rel_filebase": bool(flags & PACK_REL_FILEBASE)},
                        "dir_offset": dir_offset, "file_count": None, "entries": cap_list([], 0), "native_listing": False})
            return out
        out["flags"] = {"dir_encrypted": bool(flags & PACK_DIR_ENCRYPTED), "rel_filebase": bool(flags & PACK_REL_FILEBASE)}
        out["file_base"] = file_base
        count = struct.unpack_from("<I", f.read(4), 0)[0]
        out["file_count"] = count
        min_entry = 4 + 4 + 16 + 16 + (4 if ver >= 2 else 0)
        if count * min_entry > max(size - start, 0):
            raise ValueError(f"PCK declares {count} files, more than the file can hold")
        if flags & PACK_DIR_ENCRYPTED:
            out.update({"native_listing": False, "entries": cap_list([], 0),
                        "parse_notes": notes + ["directory is encrypted: a 64-hex key is required to list or recover"]})
            return out
        if not list_entries:
            out["native_listing"] = False
            return out
        entries: list[dict[str, Any]] = []
        encrypted_files = 0
        truncated = False
        for i in range(count):
            if i >= max_entries:
                truncated = True
                break
            raw = f.read(4)
            if len(raw) < 4:
                raise ValueError("truncated PCK directory")
            plen = struct.unpack("<I", raw)[0]
            if plen > 4096:
                raise ValueError(f"path length {plen} out of range in directory entry {i}")
            pb = f.read(plen)
            rest = f.read(8 + 8 + 16 + (4 if ver >= 2 else 0))
            if len(pb) < plen or len(rest) < 32:
                raise ValueError("truncated PCK directory")
            off, sz = struct.unpack_from("<QQ", rest, 0)
            eflags = struct.unpack_from("<I", rest, 32)[0] if ver >= 2 else 0
            if eflags & 1:
                encrypted_files += 1
            base = (start if (flags & PACK_REL_FILEBASE) else 0) + file_base if ver >= 2 else start
            entries.append({"path": pb.rstrip(b"\0").decode("utf-8", "replace"), "size": sz,
                            "encrypted": bool(eflags & 1), "in_bounds": base + off + sz <= size})
        out.update({"native_listing": True, "entries": cap_list(entries, LIST_CAP), "entries_truncated": truncated,
                    "encrypted_files": encrypted_files, "_all_entries": entries})
    return out


def summarize_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    paths = [e["path"] for e in entries]
    low = [p.lower() for p in paths]
    gd_text = [p for p in paths if p.lower().endswith(".gd")]
    bytecode = [p for p in paths if p.lower().endswith(SCRIPT_BYTECODE_EXT)]
    gdext = [p for p in paths if p.lower().endswith(".gdextension")]
    native_libs = [p for p in paths if p.lower().endswith((".so", ".dll", ".dylib", ".wasm", ".framework"))]
    return {"files": len(paths), "scripts_text_gd": len(gd_text), "scripts_bytecode": len(bytecode),
            "scripts_encrypted_bytecode": sum(1 for p in bytecode if p.lower().endswith(".gde")),
            "has_project_binary": any(p.endswith("project.binary") for p in low),
            "has_import_files": any(p.endswith(".import") for p in low),
            "gdextension_files": gdext[:50], "native_libraries": native_libs[:50],
            "csharp_files": sum(1 for p in low if p.endswith((".cs", ".csproj", ".sln")))}


# =====================================================================================================================
# project.godot
# =====================================================================================================================
def parse_project_godot(text: str) -> dict[str, Any]:
    sections: dict[str, dict[str, str]] = {"": {}}
    cur = ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
            sections.setdefault(cur, {})
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            sections.setdefault(cur, {})[k.strip()] = v.strip()
    app = sections.get("application", {})

    def unq(v: str | None) -> str | None:
        return v.strip().strip('"') if v else None

    feats = re.findall(r'"([^"]*)"', app.get("config/features", ""))
    return {"config_version": sections[""].get("config_version"), "name": unq(app.get("config/name")),
            "main_scene": unq(app.get("run/main_scene")), "features": feats,
            "feature_engine_version": next((f for f in feats if re.fullmatch(r"\d+\.\d+(\.\d+)?", f)), None),
            "autoloads": sorted(sections.get("autoload", {}).keys()), "has_dotnet_section": "dotnet" in sections,
            "sections": sorted(k for k in sections if k)}


# =====================================================================================================================
# GDRE output parsing
# =====================================================================================================================
def clean_lines(text: str) -> list[str]:
    out = []
    for ln in re.split(r"[\r\n]+", text):
        ln = ln.rstrip()
        if not ln.strip() or _PROGRESS_RE.search(ln):
            continue
        out.append(re.sub(r"\x1b\[[0-9;]*m", "", ln))
    return out


def parse_gdre_output(text: str) -> dict[str, Any]:
    lines = clean_lines(text)
    res: dict[str, Any] = {"totals": {}, "scripts_not_decompiled": [], "failed_conversions": [], "not_converted_files": [],
                           "unsupported_resource_types": [], "errors": [], "warnings": [], "fatal": [],
                           "engine_version": None, "bytecode_revision": None, "tool_version": None,
                           "verified_files": None, "extracted_files": None, "checksum_errors": 0, "imported_files": None,
                           "recovery_finished": False, "pck_loaded": False, "export_report_present": False}
    mode = None
    errs: Counter[str] = Counter()
    warns: Counter[str] = Counter()
    for ln in lines:
        m = re.match(r"GDRE Tools v(\S+)", ln)
        if m:
            res["tool_version"] = m.group(1)
        m = re.match(r"Detected Engine Version:\s*(\S+)", ln)
        if m:
            res["engine_version"] = m.group(1)
        m = re.match(r"Detected Bytecode Revision:\s*(.+)", ln)
        if m:
            res["bytecode_revision"] = m.group(1).strip()
        m = re.match(r"Verified (\d+) files, (.+)", ln)
        if m:
            res["verified_files"] = int(m.group(1))
            if not m.group(2).startswith("no errors"):
                res["checksum_errors"] = int(re.match(r"(\d+)", m.group(2)).group(1)) if re.match(r"(\d+)", m.group(2)) else 1
        m = re.match(r"Extracted (\d+) files", ln)
        if m:
            res["extracted_files"] = int(m.group(1))
        m = re.match(r"Loaded (\d+) imported files", ln)
        if m:
            res["imported_files"] = int(m.group(1))
        if ln.startswith("Successfully loaded PCK"):
            res["pck_loaded"] = True
        if ln.startswith("Recovery finished"):
            res["recovery_finished"] = True
        if "EXPORT REPORT" in ln:
            res["export_report_present"] = True
        m = re.match(r"^([A-Za-z\- ]+?):\s+(\d+)\s*$", ln)
        if m and m.group(1) in _TOTAL_KEYS:
            res["totals"][_TOTAL_KEYS[m.group(1)]] = int(m.group(2))
            continue
        if ln.startswith("The following scripts were not decompiled"):
            mode = "scripts"
            continue
        if ln.startswith("The following files were not converted"):
            mode = "notconv"
            continue
        if ln.startswith("Failed conversions:"):
            mode = "failed"
            continue
        if ln.startswith("Unsupported Resources Detected"):
            mode = "unsupported"
            continue
        if ln.startswith("------") or ln.startswith("*****") or ln.startswith("-----"):
            mode = None if mode in ("scripts", "notconv", "failed") else mode
            continue
        if mode == "scripts" and ln.startswith("res://"):
            res["scripts_not_decompiled"].append(ln.strip())
        elif mode == "notconv" and ln.strip().startswith("res://"):
            res["not_converted_files"].append(ln.strip())
        elif mode == "failed" and ln.startswith("* "):
            res["failed_conversions"].append({"resource": ln[2:].strip(), "errors": []})
        elif mode == "failed" and res["failed_conversions"] and ln.startswith("  * "):
            res["failed_conversions"][-1]["errors"].append(ln.strip()[2:])
        elif mode == "unsupported" and ln.strip().startswith("- Resource Type:"):
            res["unsupported_resource_types"].append(ln.strip()[2:])
        if ln.startswith("ERROR:"):
            errs[ln[6:].strip()] += 1
        elif ln.startswith("WARNING:"):
            warns[ln[8:].strip()] += 1
        if "FATAL ERROR" in ln or ln.startswith("Error: Failed to open") or ln.startswith("Error: failed to extract"):
            res["fatal"].append(ln.strip())
        if "MD5 checksum failed" in ln:
            if "not proceeding" in ln:
                res["fatal"].append(ln.strip())
            else:                                     # "...but --ignore_checksum_errors specified, proceeding anyway..."
                res["checksum_errors"] += 1
    res["errors"] = [{"message": k, "count": v} for k, v in errs.most_common(100)]
    res["warnings"] = [{"message": k, "count": v} for k, v in warns.most_common(50)]
    res["error_line_total"] = sum(errs.values())
    res["gdextension_library_errors"] = [e["message"] for e in res["errors"] if "gdextension libraries" in e["message"]]
    return res


def _gdextension_info(root: Path, files: list[str]) -> list[dict[str, Any]]:
    out = []
    for rel in files:
        if not rel.lower().endswith(".gdextension"):
            continue
        try:
            txt = (root / rel).read_text("utf-8", "replace")[:65536]
        except OSError:
            continue
        libs: dict[str, str] = {}
        in_libs = False
        for ln in txt.splitlines():
            s = ln.strip()
            if s.startswith("["):
                in_libs = s == "[libraries]"
            elif in_libs and "=" in s:
                k, v = s.split("=", 1)
                libs[k.strip()] = v.strip().strip('"')
        missing = []
        for plat, res_path in libs.items():
            rp = res_path[len("res://"):] if res_path.startswith("res://") else res_path
            if not (root / rp).is_file():
                missing.append({"platform": plat, "library": res_path})
        out.append({"config": rel, "libraries": libs, "missing_libraries": missing})
    return out


def _tool_home(settings: Settings) -> Path:
    h = Path(settings.data_dir) / "tool-home" / "gdre"
    h.mkdir(parents=True, exist_ok=True)
    return h


def _tool_env(settings: Settings) -> dict[str, str]:
    """GDRE (Godot) needs a writable HOME for its user dir; point everything at an app-owned directory."""
    home = _tool_home(settings)
    env = dict(os.environ)
    env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / ".local" / "share"), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache")})
    if os.name == "nt":
        env.update({"APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
                    "USERPROFILE": str(home)})
        for d in (home / "AppData" / "Roaming", home / "AppData" / "Local"):
            d.mkdir(parents=True, exist_ok=True)
    return env


_PROBE_CACHE: dict[tuple[str, float], tuple[str | None, str]] = {}
_KEY_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


class GDREBackend(BackendAdapter):
    backend_id = BACKEND_ID

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # -- discovery -----------------------------------------------------------------------------------------------
    def find_tool(self) -> Path | None:
        return discover_executable(self.settings, ["gdre_tools.x86_64", "gdre_tools", "gdre_tools.arm64", "Godot_RE_Tools", "gdre_tools.windows"],
                                   subdirs=["gdre", "gdsdecomp", "gdre_tools"], bin_subpaths=("", "bin"))

    def _zip_integrity(self) -> tuple[str, str]:
        for z in sorted(Path(self.settings.tools_dir).glob("GDRE_tools-v*.zip")):
            try:
                sha = sha256_of(z)
            except OSError:
                continue
            if z.name == PINNED_ZIP and sha == PINNED_ZIP_SHA256:
                return sha, f"{z.name} matches the pinned sha256"
            return sha, f"{z.name} sha256 does not match the pinned {PINNED_ZIP_SHA256[:12]}..."
        return "", "no release archive found next to the tools to verify"

    def probe(self) -> BackendInfo:
        try:
            return self._probe()
        except Exception as e:  # noqa: BLE001 - probe() must never raise
            return failed_probe_info(BACKEND_ID, "GDRE Tools (Godot project recovery)", TOOL_NAME, e, INSTALL_HINT)

    def _probe(self) -> BackendInfo:
        exe = self.find_tool()
        if exe is None:
            tool: ToolProbe = probe_error(TOOL_NAME, "gdre_tools not found in tools dir or PATH", INSTALL_HINT, prerequisites=[],
                                          license=LICENSE, source=SOURCE_URL, pinned=PINNED_VERSION)
        else:
            tool = self._probe_exe(exe)
        return BackendInfo(
            backend_id=BACKEND_ID, title="GDRE Tools (Godot project recovery)",
            formats=["godot_pck"], platforms=["linux", "windows", "macos"], profiles=["godot"],
            operations=[
                Operation("detect", "Find/parse a Godot PCK (standalone, trailer-embedded or pck section)", {"path": "path"}, {"detect": "dict"}),
                Operation("recover", "Headless full-project recovery with a gap report", {"pck": "path", "out_dir": "path"}, {"recovery_report": "dict"}),
            ],
            tools=[tool], resources={"typical_seconds": "4-600", "ram_mb": 500, "next_action": getattr(tool, "next_action", "")},
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
                r = run_bounded(None, [str(exe), "--headless", "--version"], limits=self.settings.limits, env=_tool_env(self.settings),
                                cwd=str(_tool_home(self.settings)), timeout=60)
                m = re.search(r"v(\d+\.\d+\.\d+\S*)", r.text)
                if m:  # note: gdre_tools --version exits 1 even on success, so the banner is the signal
                    ver = m.group(1)
                else:
                    detail = (r.err_text.strip() or r.text.strip() or f"exit code {r.returncode}")[:500]
            except (StageError, OSError) as e:
                detail = f"{type(e).__name__}: {e}"
            _PROBE_CACHE[key] = (ver, detail)
        integrity, idetail = self._zip_integrity()
        if ver is None:
            return RecoveryToolProbe(TOOL_NAME, Availability.DETECTED, path=str(exe), detail=f"found but --version failed: {detail}",
                                     license=LICENSE, source=SOURCE_URL, pinned=PINNED_VERSION, integrity=integrity,
                                     next_action="Check that gdre_tools.pck and libGodotMonoDecompNativeAOT sit next to the executable and that HOME is writable")
        note = "" if ver == PINNED_VERSION else f"version differs from pinned {PINNED_VERSION}; "
        return RecoveryToolProbe(TOOL_NAME, Availability.INSTALLED, path=str(exe), version=ver, detail=note + idetail,
                                 license=LICENSE, source=SOURCE_URL, pinned=PINNED_VERSION, integrity=integrity,
                                 next_action="" if not note else f"install the pinned version: {INSTALL_HINT}")

    def smoke(self) -> ToolProbe:
        tool = self.probe().tools[0]
        if tool.availability not in (Availability.INSTALLED, Availability.USABLE) or not tool.path:
            return tool
        with tempfile.TemporaryDirectory(prefix="rs-gdre-smoke-") as td:
            pck = Path(td) / "smoke.pck"
            pck.write_bytes(build_probe_pck([("res://smoke.txt", b"gdre smoke\n")]))
            out = Path(td) / "out"
            try:
                r = run_bounded(None, [tool.path, "--headless", f"--recover={pck}", f"--output={out}"], limits=self.settings.limits,
                                env=_tool_env(self.settings), cwd=str(_tool_home(self.settings)), timeout=180)
            except (StageError, OSError) as e:
                tool.detail = f"smoke failed: {e}"
                return tool
            f = out / "smoke.txt"
            if f.is_file() and f.read_bytes() == b"gdre smoke\n":
                tool.availability = Availability.USABLE
                tool.detail = "recovered a built-in one-file PCK"
            else:
                tool.detail = f"smoke failed: exit {r.returncode}: {(r.err_text or r.text)[-300:]}"
        return tool

    # -- operations ----------------------------------------------------------------------------------------------
    def op_detect(self, ctx: Any, path: str, **kw: Any) -> OperationResult:
        return self.detect(path, ctx=ctx, **kw)

    def op_recover(self, ctx: Any, pck: str, out_dir: str, **kw: Any) -> OperationResult:
        return self.recover(pck, out_dir, ctx=ctx, **kw)

    def _tool_version(self) -> tuple[Path | None, str | None]:
        exe = self.find_tool()
        if exe is None:
            return None, None
        return exe, self._probe_exe(exe).version

    def detect(self, path: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
               module_id: str | None = None) -> OperationResult:
        return self._detect(path, ctx=ctx, studio=studio, case_id=case_id, module_id=module_id)[0]

    def _detect(self, path: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
                module_id: str | None = None) -> tuple[OperationResult, list[dict[str, Any]]]:
        p = Path(path)
        if p.is_dir():
            has = (p / "project.godot").is_file() or (p / "project.binary").is_file()
            return OperationResult(ok=True, data={"kind": "project_dir" if has else "none", "path": str(p),
                                                  "note": "directory input; GDRE can recover an extracted project dir" if has else "no project.godot found"}), []
        if not p.is_file():
            return OperationResult(ok=False, error=f"not found: {p}"), []
        try:
            info = read_pck_header(p, max_entries=self.settings.limits.max_archive_entries)
        except (ValueError, OSError, struct.error) as e:
            return OperationResult(ok=False, error=f"malformed PCK: {e}", data={"kind": "malformed", "path": str(p)}), []
        entries = info.pop("_all_entries", [])
        info["summary"] = summarize_entries(entries) if entries else None
        sha = sha256_file(p)
        info["sha256"] = sha
        info["tool"] = {"name": TOOL_NAME, "version": self._tool_version()[1]}
        eids: list[str] = []
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "godot.detect", f"Godot PCK detection: {p.name}", info, module_id=module_id,
                              inputs={"op": "detect", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": "native-pck-parser",
                                      "tool_version": SCHEMA_VERSION, "module_sha256": sha}, producer=BACKEND_ID)
        if eid:
            eids.append(eid)
        return (OperationResult(ok=info["kind"] != "none", data=info, evidence_ids=eids, truncated=bool(info.get("entries_truncated") or info.get("entries", {}).get("truncated")),
                                error=None if info["kind"] != "none" else "no Godot PCK found"), entries)

    def recover(self, pck: Path | str, out_dir: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
                module_id: str | None = None, source_root: Path | str | None = None, key: str | None = None,
                timeout: float = 900, scripts_only: bool = False, ignore_checksum_errors: bool = False) -> OperationResult:
        src, out = Path(pck), Path(out_dir)
        if not src.exists():
            return OperationResult(ok=False, error=f"input not found: {src}")
        if key is not None and not _KEY_RE.match(key):
            return OperationResult(ok=False, error="key must be a 64-character hex string")
        try:
            if source_root is not None:
                assert_output_not_in_source(out, Path(source_root))
            if is_within(resolve_final(src), resolve_final(out)):
                raise PathPolicyError(f"output directory {out} contains the input {src}")
        except PathPolicyError as e:
            return OperationResult(ok=False, error=f"path policy: {e}")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            return OperationResult(ok=False, error=f"output directory {out} exists and is not empty; refusing to mix outputs")
        exe, ver = self._tool_version()
        if exe is None or ver is None:
            return OperationResult(ok=False, error=f"gdre_tools is not usable on this host. next_action: {INSTALL_HINT}",
                                   data={"next_action": INSTALL_HINT})
        det, entries_all = self._detect(src) if src.is_file() else (OperationResult(ok=True, data={"kind": "project_dir"}), [])
        pck_info = dict(det.data)
        pck_info.pop("entries", None)
        sha = pck_info.get("sha256") or (sha256_file(src) if src.is_file() else "dir:" + src.name)
        notes: list[str] = []
        if not det.ok and src.is_file():
            notes.append("no PCK structure detected natively; passing the file to GDRE anyway (it also accepts APK/EXE variants)")
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(exe), "--headless", f"--recover={src}", f"--output={out}"]
        if key:
            cmd.append(f"--key={key}")
        if scripts_only:
            cmd.append("--scripts-only")
        if ignore_checksum_errors:
            cmd.append("--ignore-checksum-errors")
        try:
            r = run_bounded(ctx, cmd, limits=self.settings.limits, env=_tool_env(self.settings), cwd=str(_tool_home(self.settings)),
                            timeout=timeout)
        except StageError as e:
            return OperationResult(ok=False, error=str(e))
        stdout_text = r.text + "\n" + r.err_text
        parsed = parse_gdre_output(stdout_text)
        log_path = out / "gdre_export.log"
        log_excerpt = ""
        if log_path.is_file():
            log_excerpt = log_path.read_bytes()[-LOG_EXCERPT_BYTES:].decode("utf-8", "replace")
            if not parsed["export_report_present"] or not parsed["recovery_finished"]:
                lp = parse_gdre_output(log_excerpt)
                for k in ("totals", "scripts_not_decompiled", "failed_conversions", "not_converted_files", "unsupported_resource_types",
                          "errors", "engine_version", "bytecode_revision", "verified_files", "extracted_files", "recovery_finished",
                          "export_report_present", "pck_loaded", "fatal", "gdextension_library_errors"):
                    if not parsed.get(k) and lp.get(k):
                        parsed[k] = lp[k]
        # --- scan what is on disk ---------------------------------------------------------------------------------
        files: list[str] = []
        total_bytes = 0
        truncated_scan = False
        if out.is_dir():
            for dp, dn, fn in os.walk(out, followlinks=False):
                for n in fn:
                    fp = Path(dp) / n
                    if fp.is_symlink():
                        continue
                    if len(files) >= self.settings.limits.max_inventory_files:
                        truncated_scan = True
                        break
                    files.append(fp.relative_to(out).as_posix())
                    try:
                        total_bytes += fp.stat().st_size
                    except OSError:
                        pass
                if truncated_scan:
                    break
        files.sort()
        user_files = [f for f in files if not f.startswith((".godot/", ".autoconverted/")) and f != "gdre_export.log"]
        by_ext = Counter(Path(f).suffix.lower() or "(none)" for f in user_files)
        gd_out = [f for f in user_files if f.endswith(".gd")]
        left_bytecode = [f for f in user_files if f.lower().endswith(SCRIPT_BYTECODE_EXT)]
        originals = [f for f in files if f.startswith(".autoconverted/") and f.lower().endswith(SCRIPT_BYTECODE_EXT)]
        pck_text = {e["path"] for e in entries_all if e["path"].lower().endswith(".gd")}
        pck_bc = [e["path"] for e in entries_all if e["path"].lower().endswith(SCRIPT_BYTECODE_EXT)]
        script_items: list[dict[str, str]] = []
        failed_set = set(parsed["scripts_not_decompiled"])
        if entries_all:
            for pth in sorted(pck_text):
                script_items.append({"path": pth, "source": "pck_text", "status": "extracted_as_is"})
            for pth in sorted(pck_bc):
                gd = pth.rsplit(".", 1)[0] + ".gd"
                ok = gd[len("res://"):] in set(gd_out) and pth not in failed_set
                script_items.append({"path": pth, "source": "bytecode", "status": "decompiled" if ok else "not_decompiled"})
        else:
            for f in sorted(gd_out):
                orig = ".autoconverted/" + f[:-3] + ".gdc"
                script_items.append({"path": "res://" + f, "source": "bytecode" if orig in originals else "pck_text_or_unknown",
                                     "status": "decompiled" if orig in originals else "present"})
            for f in sorted(failed_set):
                script_items.append({"path": f, "source": "bytecode", "status": "not_decompiled"})
        sc_decomp = sum(1 for s in script_items if s["status"] == "decompiled")
        sc_fail = sum(1 for s in script_items if s["status"] == "not_decompiled")
        sc_text = sum(1 for s in script_items if s["status"] in ("extracted_as_is", "present"))
        project: dict[str, Any] | None = None
        pg = out / "project.godot"
        if pg.is_file():
            project = parse_project_godot(pg.read_text("utf-8", "replace")[:1_000_000])
        engine_version = parsed["engine_version"] or pck_info.get("engine_version") or (project or {}).get("feature_engine_version")
        gdext = _gdextension_info(out, user_files)
        summ = summarize_entries(entries_all) if entries_all else None
        csharp = bool(project and project.get("has_dotnet_section")) or any(f.endswith((".cs", ".csproj", ".sln")) for f in user_files) \
            or bool(summ and summ["csharp_files"])
        recovered = bool(parsed["pck_loaded"] and parsed["recovery_finished"] and not parsed["fatal"] and out.is_dir() and user_files)
        needs_key = bool(pck_info.get("flags", {}).get("dir_encrypted")) or bool(pck_info.get("encrypted_files"))
        gaps = {
            "scripts_not_decompiled": sorted(failed_set)[:200],
            "scripts_not_decompiled_count": max(len(failed_set), parsed["totals"].get("scripts_not_decompiled", 0), sc_fail),
            "bytecode_left_undecompiled_on_disk": left_bytecode[:200],
            "failed_conversions": parsed["failed_conversions"][:200],
            "failed_conversions_count": parsed["totals"].get("failed_conversions", len(parsed["failed_conversions"])),
            "not_converted_files": parsed["not_converted_files"][:200],
            "not_converted_count": parsed["totals"].get("not_converted", len(parsed["not_converted_files"])),
            "lossy_conversions": parsed["totals"].get("lossy", 0),
            "unsupported_resource_types": parsed["unsupported_resource_types"][:50],
            "native_extensions": gdext,
            "native_extensions_missing_libraries": sum(len(g["missing_libraries"]) for g in gdext),
            "gdextension_library_errors": parsed["gdextension_library_errors"][:20],
            "encryption": {"needs_key": needs_key and not key, "key_supplied": bool(key),
                           "encrypted_files": pck_info.get("encrypted_files", 0)},
            "csharp_assemblies_outside_pck": {"csharp_project": csharp,
                                              "note": "C# game code is compiled to a .NET assembly shipped beside the exe, not inside the PCK; "
                                                      "run the ILSpy backend on that assembly" if csharp else ""},
            "checksum_errors": parsed["checksum_errors"],
            "errors": parsed["errors"][:100], "error_line_total": parsed["error_line_total"], "fatal": parsed["fatal"][:20],
        }
        gaps_found = bool(gaps["scripts_not_decompiled_count"] or gaps["failed_conversions_count"] or gaps["not_converted_count"]
                          or gaps["native_extensions"] or gaps["native_extensions_missing_libraries"] or gaps["fatal"]
                          or gaps["encryption"]["needs_key"] or csharp or gaps["checksum_errors"] or left_bytecode
                          or parsed["error_line_total"])
        report = {
            "schema": SCHEMA_VERSION, "status": ("failed" if not recovered else "partial" if gaps_found else "ok"),
            "tool": {"name": TOOL_NAME, "version": ver, "command": f"gdre_tools --headless --recover=<input> --output=<out>"
                     + (" --key=<redacted>" if key else "") + (" --scripts-only" if scripts_only else "")
                     + (" --ignore-checksum-errors" if ignore_checksum_errors else "")},
            "module": {"path": str(src), "sha256": sha, "kind": pck_info.get("kind"), "pack_version": pck_info.get("pack_version"),
                       "pck_start": pck_info.get("pck_start"), "dir_encrypted": pck_info.get("flags", {}).get("dir_encrypted")},
            "engine": {"version": engine_version, "from_gdre": parsed["engine_version"], "from_pck_header": pck_info.get("engine_version"),
                       "from_project_features": (project or {}).get("feature_engine_version"), "bytecode_revision": parsed["bytecode_revision"]},
            "project": project,
            "recovered_resources": {"files_total": len(files), "files_user_visible": len(user_files), "bytes": total_bytes,
                                    "by_extension": dict(by_ext.most_common(30)), "scan_truncated": truncated_scan,
                                    "gdre_verified_files": parsed["verified_files"], "gdre_extracted_files": parsed["extracted_files"],
                                    "gdre_converted": parsed["totals"].get("converted", 0),
                                    "pck_entries_listed_natively": len(entries_all) if entries_all else None},
            "scripts": {"text_gd": sc_text, "bytecode_decompiled": sc_decomp, "bytecode_not_decompiled": sc_fail,
                        "originals_kept_under_.autoconverted": len(originals), "items": cap_list(script_items, LIST_CAP)},
            "gdre_totals": parsed["totals"], "gaps": gaps, "gaps_found": gaps_found,
            "files": cap_list(user_files, LIST_CAP), "notes": notes + pck_info.get("parse_notes", []),
            "exit_code": r.returncode, "output_truncated": r.truncated, "log_excerpt": log_excerpt[-8192:],
            "claims": "Recovered files and GDRE's own conversion counts only. Gaps are listed explicitly; behavioural equivalence with the original game is not asserted.",
            "equivalence_claimed": False,
        }
        inputs = {"op": "recover", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": TOOL_NAME, "tool_version": ver,
                  "module_sha256": sha, "scripts_only": scripts_only, "ignore_checksum_errors": ignore_checksum_errors,
                  "key_fingerprint": hashlib.sha256(key.encode()).hexdigest()[:12] if key else None}
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "godot.recovery_report", f"GDRE recovery report: {src.name}", report,
                              module_id=module_id, inputs=inputs, producer=BACKEND_ID)
        trunc = truncated_scan or report["files"]["truncated"] or report["scripts"]["items"]["truncated"] or r.truncated
        return OperationResult(ok=recovered, data={"recovery_report": report, "out_dir": str(out)}, evidence_ids=[eid] if eid else [],
                               truncated=bool(trunc),
                               error=None if recovered else ("; ".join(parsed["fatal"][:3]) or f"GDRE did not complete a recovery (exit {r.returncode})"))
