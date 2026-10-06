"""JS / web / Electron inspection and bounded extraction (pure Python; node is optional).

What this backend genuinely does
- ``inspect(root)``: walks a directory (or a single ``.asar``) without following links, finds package.json files, Electron
  signals and the main entry, ``app.asar`` archives (listed natively), source maps (``.map`` files, ``sourceMappingURL``
  comments, inline ``data:`` maps), bundler fingerprints (webpack/vite/esbuild/parcel/browserify/next, heuristic string
  markers), service workers and web manifests. Every scan is bounded; hitting a bound is reported as ``truncated``.
- ``extract(root, out_dir)``: writes ``tree/`` (loose web files), ``asar/<name>/`` (asar contents incl. ``.unpacked``
  siblings) and ``sources/<map>/`` (``sourcesContent`` embedded in source maps) under a shared byte/entry budget with
  escape-safe paths, plus ``extraction_manifest.json``.

What it does NOT do: deobfuscate or reconstruct code that has no source map. Minified bundles without ``sourcesContent`` are
copied as-is and reported as such; only ``sourcesContent`` is labelled as original author source (provenance
``sourcemap.sourcesContent``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Settings, get_settings
from ..ids import sha256_file
from ..jobs.runner import StageError
from ..paths import PathPolicyError, assert_output_not_in_source, is_within, resolve_final
from .archive import (ArchiveError, ArchiveLimits, AsarReader, ExtractionReport, RecoveryToolProbe, SafeWriter, cap_list,
                      discover_executable, extract_archive, list_archive, read_chunks, record_evidence, resolve_studio,
                      run_bounded, verify_asar_integrity)

BACKEND_ID = "jsweb"
ENGINE_NAME = "jsweb-native"
ENGINE_VERSION = "1"
SCHEMA_VERSION = 1
ASAR_PINNED = "4.3.1"
ASAR_INTEGRITY = "sha512-6pI64z/tMSBkUTZLQFYhs2YiB7fS9sKA6gy7/2nH1Ju7GGwT32UPsj4Mxsws5NMmtR6C0zYdXVxEU/rGqJRCJA=="
ASAR_INSTALL_HINT = f"npm install --save-exact @electron/asar@{ASAR_PINNED} --prefix <tools_dir>/asar   (optional: only for cross-checks; inspection is native)"
NODE_INSTALL_HINT = "Install Node.js 22 LTS (optional; only needed to build/run the reconstructed JS target, not for inspection)"

LIST_CAP = 2000
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 4 * 1024
MAX_PACKAGE_JSON = 1 << 20
MAX_MAP_BYTES = 64 * 1024 * 1024
MAP_READ_BUDGET = 256 * 1024 * 1024
MAX_SCAN_FILES = 3000
MAX_MAPS = 500
MAX_ASARS = 20
HASH_BUDGET = 1024 * 1024 * 1024
WEB_EXT = {".js", ".mjs", ".cjs", ".map", ".html", ".htm", ".css", ".json", ".webmanifest", ".svg", ".txt", ".wasm", ".ts", ".tsx", ".jsx", ".vue", ".md"}
JS_EXT = {".js", ".mjs", ".cjs"}

_SMAP_JS_RE = re.compile(rb"//[#@]\s*sourceMappingURL=([^\s'\"]+)")
_SMAP_CSS_RE = re.compile(rb"/\*[#@]\s*sourceMappingURL=([^\s*'\"]+)\s*\*/")
_SW_REG_RE = re.compile(rb"serviceWorker\s*\.\s*register\(\s*['\"]([^'\"]+)")
_SW_EVENT_RE = re.compile(rb"addEventListener\(\s*['\"](install|activate|fetch|push|message)['\"]")
_SW_HINT_RE = re.compile(rb"skipWaiting|clients\.claim|caches\.open|workbox|importScripts")
_LOADFILE_RE = re.compile(rb"loadFile\(\s*['\"]([^'\"]+)")
_LOADURL_RE = re.compile(rb"loadURL\(\s*['\"]([^'\"]+)")
_PRELOAD_RE = re.compile(rb"preload\s*:\s*[^,}]*?['\"]([^'\"]+\.[cm]?js)['\"]")
_SCRIPT_SRC_RE = re.compile(rb"<script[^>]+src=['\"]([^'\"]+)", re.I)
_LINK_MANIFEST_RE = re.compile(rb"<link[^>]+rel=['\"]manifest['\"][^>]*href=['\"]([^'\"]+)|<link[^>]+href=['\"]([^'\"]+)['\"][^>]*rel=['\"]manifest['\"]", re.I)

BUNDLER_MARKERS: dict[str, list[tuple[str, re.Pattern[bytes]]]] = {
    "webpack": [("__webpack_require__", re.compile(rb"__webpack_require__")), ("webpackChunk/webpackJsonp", re.compile(rb"webpackChunk|webpackJsonp")),
                ("/******/ banner", re.compile(rb"/\*{6}/")), ("__webpack_modules__", re.compile(rb"__webpack_modules__"))],
    "vite": [("import.meta.env", re.compile(rb"import\.meta\.env")), ("__vite__mapDeps", re.compile(rb"__vite__mapDeps")),
             ("modulepreload-polyfill", re.compile(rb"modulepreload-polyfill|vite/modulepreload"))],
    "esbuild": [("__commonJS", re.compile(rb"__commonJS")), ("__toESM", re.compile(rb"__toESM")), ("__defProp", re.compile(rb"__defProp")),
                ("path banner comments", re.compile(rb"(?m)^// (?:node_modules|src)/"))],
    "parcel": [("parcelRequire", re.compile(rb"parcelRequire")), ("$parcel$", re.compile(rb"\$parcel\$"))],
    "browserify": [("browserify prelude", re.compile(rb"require=function\s+\w\(\w,\w,\w\)|\(function\(\)\{function \w\(\w,\w,\w\)"))],
    "next.js": [("__next_f", re.compile(rb"__next_f")), ("/_next/static/", re.compile(rb"/_next/static/"))],
}
ELECTRON_FILES = ("resources.pak", "chrome_100_percent.pak", "chrome_200_percent.pak", "icudtl.dat", "v8_context_snapshot.bin",
                  "libffmpeg.so", "ffmpeg.dll", "snapshot_blob.bin", "default_app.asar", "electron.asar")


# =====================================================================================================================
# helpers
# =====================================================================================================================
def sanitize_source_path(name: str, index: int = 0) -> tuple[str, bool]:
    """Turn a source-map ``sources`` entry into a safe relative path.

    Returns (path, neutralized). ``neutralized`` is True only when something unsafe was altered: ``..`` segments dropped,
    absolute/drive prefixes stripped, control or Windows-invalid characters replaced, or over-long segments cut. Plain
    scheme prefixes (``webpack:///``), ``./`` and query strings are normalised without setting the flag.
    """
    n = name.split("?", 1)[0].split("#", 1)[0]
    n = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*:/{2,}", "", n)       # webpack:///, file:///, ng://
    n = n.replace("\\", "/")
    neutral = False
    if re.match(r"^[A-Za-z]:", n):
        n, neutral = n[2:], True
    if n.startswith("/"):
        neutral = True
    parts: list[str] = []
    for seg in n.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            neutral = True
            continue
        clean = re.sub(r'[\x00-\x1f<>:"|?*]', "_", seg).rstrip(". ")
        if clean != seg or len(clean) > 120:
            neutral = True
        if clean:
            parts.append(clean[:120])
    path = "/".join(parts)
    if not path:
        return f"unnamed_{index}.txt", True
    return path, neutral


def tree_digest(entries: list[tuple[str, Path]], budget: int = HASH_BUDGET) -> tuple[str, bool]:
    """sha256 over sorted (relpath, size, content-sha256). Beyond ``budget`` bytes files contribute size only (partial=True)."""
    h = hashlib.sha256()
    partial = False
    left = budget
    for rel, p in sorted(entries):
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz <= left:
            fh = sha256_file(p)
            left -= sz
        else:
            fh = f"size-only:{sz}"
            partial = True
        h.update(f"{rel}\0{sz}\0{fh}\n".encode())
    return h.hexdigest(), partial


def build_asar(files: dict[str, bytes], *, with_integrity: bool = False) -> bytes:
    """Minimal asar writer (used by smoke(); tests use the real @electron/asar CLI and their own malformed builders)."""
    tree: dict[str, Any] = {"files": {}}
    data = b""
    for name in sorted(files):
        node = tree
        parts = name.split("/")
        for d in parts[:-1]:
            node = node["files"].setdefault(d, {"files": {}})
        content = files[name]
        entry: dict[str, Any] = {"size": len(content), "offset": str(len(data))}
        if with_integrity:
            entry["integrity"] = {"algorithm": "SHA256", "hash": hashlib.sha256(content).hexdigest(), "blockSize": 4194304,
                                  "blocks": [hashlib.sha256(content).hexdigest()]}
        node["files"][parts[-1]] = entry
        data += content
    js = json.dumps(tree, separators=(",", ":")).encode()
    padded = js + b"\0" * ((-len(js)) % 4)
    payload = 4 + len(padded)
    return struct.pack("<IIII", 4, payload + 4, payload, len(js)) + padded + data


@dataclass
class _VF:
    rel: str                   # display path; asar members look like "resources/app.asar!/main.js"
    size: int
    path: Path | None = None   # disk file
    asar: int | None = None    # index into the asar reader list
    member: str | None = None


@dataclass
class _Scan:
    root: Path
    root_kind: str
    files: list[_VF] = field(default_factory=list)
    asar_paths: list[tuple[str, Path]] = field(default_factory=list)
    readers: list[AsarReader] = field(default_factory=list)
    truncation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    symlinks: int = 0
    node_modules: dict[str, Any] = field(default_factory=lambda: {"dirs": 0, "packages": [], "packages_truncated": False})
    asar_reports: list[dict[str, Any]] = field(default_factory=list)
    disk_total_bytes: int = 0
    disk_files: int = 0

    def read(self, vf: _VF, n: int, offset: int = 0) -> bytes:
        if vf.path is not None:
            try:
                with open(vf.path, "rb") as f:
                    f.seek(offset)
                    return f.read(n)
            except OSError:
                return b""
        if vf.asar is not None and vf.member is not None:
            return self.readers[vf.asar].read(vf.member, n, offset) or b""
        return b""

    def close(self) -> None:
        for r in self.readers:
            r.close()


def _walk(root: Path, scan: _Scan, limits: ArchiveLimits, max_files: int) -> None:
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            scan.warnings.append(f"cannot read directory {d}: {e}")
            continue
        for e in it:
            try:
                if e.is_symlink():
                    scan.symlinks += 1
                    continue
                rel = Path(e.path).relative_to(root).as_posix()
                if e.is_dir(follow_symlinks=False):
                    if e.name == "node_modules":
                        scan.node_modules["dirs"] += 1
                        try:
                            pk = sorted(x.name for x in os.scandir(e.path) if x.is_dir(follow_symlinks=False) and not x.name.startswith("."))
                        except OSError:
                            pk = []
                        room = 500 - len(scan.node_modules["packages"])
                        scan.node_modules["packages"].extend(pk[:max(0, room)])
                        if len(pk) > room:
                            scan.node_modules["packages_truncated"] = True
                        continue
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    if scan.disk_files >= max_files:
                        if "inventory file limit" not in " ".join(scan.truncation):
                            scan.truncation.append(f"inventory file limit {max_files} reached; remaining files not inspected")
                        return
                    sz = e.stat(follow_symlinks=False).st_size
                    scan.disk_files += 1
                    scan.disk_total_bytes += sz
                    scan.files.append(_VF(rel=rel, size=sz, path=Path(e.path)))
                    if e.name.lower().endswith(".asar"):
                        scan.asar_paths.append((rel, Path(e.path)))
            except OSError as err:
                scan.warnings.append(f"cannot stat {e.path}: {err}")


def _load_asars(scan: _Scan, limits: ArchiveLimits) -> None:
    budget_entries = limits.max_entries
    for rel, p in scan.asar_paths[:MAX_ASARS]:
        entry: dict[str, Any] = {"path": rel, "size": p.stat().st_size}
        try:
            lst = list_archive(p, limits=ArchiveLimits(max_entries=max(1, budget_entries), max_expansion_bytes=limits.max_expansion_bytes),
                               format="asar")
        except ArchiveError as e:
            entry.update({"ok": False, "error": str(e)})
            scan.asar_reports.append(entry)
            continue
        entry.update({"ok": True, "entries": len(lst.members), "declared_entries": lst.declared_entries, "truncated": lst.truncated,
                      "truncation_reason": lst.truncation_reason, "total_size": lst.total_size, "errors": lst.errors[:20],
                      "unpacked_files": sum(1 for m in lst.members if m.unpacked),
                      "unpacked_dir_present": p.with_name(p.name + ".unpacked").is_dir(),
                      "symlink_members": sum(1 for m in lst.members if m.kind == "symlink")})
        if lst.truncated:
            scan.truncation.append(f"asar {rel}: {lst.truncation_reason}")
        try:
            entry["integrity"] = verify_asar_integrity(p, limits)
        except ArchiveError:
            entry["integrity"] = None
        reader = AsarReader(p, limits)
        idx = len(scan.readers)
        scan.readers.append(reader)
        entry["reader"] = idx
        for m in lst.members:
            if m.kind == "file":
                scan.files.append(_VF(rel=f"{rel}!/{m.name}", size=m.size, asar=idx, member=m.name))
        budget_entries -= len(lst.members)
        scan.asar_reports.append(entry)
    if len(scan.asar_paths) > MAX_ASARS:
        scan.truncation.append(f"more than {MAX_ASARS} asar archives; only the first {MAX_ASARS} inspected")


def _json_file(scan: _Scan, vf: _VF, limit: int = MAX_PACKAGE_JSON) -> Any:
    if vf.size > limit:
        return None
    try:
        return json.loads(scan.read(vf, limit).decode("utf-8-sig", "replace"))
    except (ValueError, RecursionError):
        return None


def _summ_package(pj: dict[str, Any]) -> dict[str, Any]:
    deps = {}
    for sec in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        v = pj.get(sec)
        if isinstance(v, dict):
            deps[sec] = {k: str(x)[:40] for k, x in list(v.items())[:200]}
    scripts = pj.get("scripts") if isinstance(pj.get("scripts"), dict) else {}
    return {"name": pj.get("name"), "version": pj.get("version"), "main": pj.get("main") if isinstance(pj.get("main"), str) else None,
            "productName": pj.get("productName"), "type": pj.get("type"), "dependencies": deps,
            "script_names": sorted(scripts)[:50], "has_build_config": isinstance(pj.get("build"), dict) or "electron-builder" in json.dumps(pj)[:200000]}


def _parse_map(raw: bytes) -> dict[str, Any] | None:
    try:
        m = json.loads(raw.decode("utf-8-sig", "replace"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(m, dict):
        return None
    if isinstance(m.get("sections"), list):   # indexed source map
        return {"_indexed": True, "version": m.get("version"), "sources": [], "sourcesContent": [], "file": m.get("file")}
    return m


def _map_summary(m: dict[str, Any]) -> dict[str, Any]:
    if m.get("_indexed"):
        return {"valid": True, "indexed": True, "version": m.get("version"), "sources": 0, "sources_with_content": 0, "sources_without_content": 0}
    srcs = m.get("sources") if isinstance(m.get("sources"), list) else []
    cont = m.get("sourcesContent") if isinstance(m.get("sourcesContent"), list) else []
    with_c = sum(1 for c in cont[:len(srcs)] if isinstance(c, str))
    return {"valid": isinstance(m.get("version"), int) and isinstance(m.get("sources"), list), "version": m.get("version"),
            "file": m.get("file") if isinstance(m.get("file"), str) else None, "source_root": m.get("sourceRoot") if isinstance(m.get("sourceRoot"), str) else None,
            "sources": len(srcs), "sources_with_content": with_c, "sources_without_content": len(srcs) - with_c}


def _resolve_map_url(bundle_rel: str, url: str) -> tuple[str, str | None]:
    """Classify a sourceMappingURL. Returns (kind, resolved_rel). kinds: inline | remote | file | escapes."""
    if url.startswith("data:"):
        return "inline", None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", url):
        return "remote", None
    base = bundle_rel.split("!/", 1)
    u = unquote(url.split("?", 1)[0])
    if len(base) == 2:
        cand = posixpath.normpath(posixpath.join(posixpath.dirname(base[1]), u))
        if cand.startswith(".."):
            return "escapes", None
        return "file", f"{base[0]}!/{cand}"
    cand = posixpath.normpath(posixpath.join(posixpath.dirname(bundle_rel), u))
    if cand.startswith("..") or posixpath.isabs(cand):
        return "escapes", None
    return "file", cand


def _decode_inline_map(url: str) -> bytes | None:
    m = re.match(r"^data:[^,]*?(;base64)?,(.*)$", url, re.S)
    if not m:
        return None
    try:
        if m.group(1):
            return base64.b64decode(m.group(2), validate=False)
        return unquote(m.group(2)).encode("utf-8")
    except (ValueError, base64.binascii.Error):
        return None


# =====================================================================================================================
# analysis
# =====================================================================================================================
def _analyse(scan: _Scan, limits: ArchiveLimits, *, max_scan_files: int, max_map_bytes: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Returns (report, map_records). map_records carry parsed maps for extract() (kept out of the report body)."""
    by_rel = {f.rel: f for f in scan.files}
    exts: dict[str, int] = {}
    for f in scan.files:
        e = Path(f.rel.split("!/")[-1]).suffix.lower() or "(none)"
        exts[e] = exts.get(e, 0) + 1
    total_bytes = sum(f.size for f in scan.files)

    # --- package.json files -------------------------------------------------------------------------------------
    packages: list[dict[str, Any]] = []
    pkg_raw: dict[str, dict[str, Any]] = {}
    for f in scan.files:
        leaf = f.rel.rsplit("/", 1)[-1]
        if leaf != "package.json":
            continue
        depth = f.rel.split("!/")[-1].count("/")
        if depth > 3:
            continue
        pj = _json_file(scan, f)
        if isinstance(pj, dict):
            pkg_raw[f.rel] = pj
            packages.append({"path": f.rel, **_summ_package(pj)})
    primary = None
    for rel, pj in pkg_raw.items():          # prefer an app root inside an asar, then the root package.json
        if "!/" in rel and rel.endswith("!/package.json") and pj.get("main"):
            primary = rel
            break
    if primary is None and "package.json" in pkg_raw:
        primary = "package.json"
    if primary is None and pkg_raw:
        primary = sorted(pkg_raw)[0]

    # --- Electron -----------------------------------------------------------------------------------------------
    signals: list[str] = []
    alldeps = {}
    if primary:
        for sec, d in _summ_package(pkg_raw[primary])["dependencies"].items():
            alldeps.update(d)
    if "electron" in alldeps:
        signals.append(f"package.json depends on electron ({alldeps['electron']})")
    if any(k.startswith(("electron-builder", "@electron-forge", "electron-packager")) for k in alldeps):
        signals.append("package.json depends on an Electron packager")
    if scan.asar_reports:
        for a in scan.asar_reports:
            if a["path"].endswith("app.asar"):
                signals.append(f"{a['path']} present")
    names = {f.rel.rsplit("/", 1)[-1] for f in scan.files}
    sig_files = [n for n in ELECTRON_FILES if n in names]
    if sig_files:
        signals.append("Electron runtime files: " + ", ".join(sig_files))
    main_info: dict[str, Any] = {"entry": None, "exists": False, "location": None, "browser_window": False, "load_file": [], "load_url": [], "preload": []}
    if primary and isinstance(pkg_raw[primary].get("main"), str):
        pkg_dir = primary.rsplit("package.json", 1)[0]
        main = pkg_raw[primary]["main"]
        cands = [main, main + ".js", posixpath.join(main, "index.js")]
        for c in cands:
            nrm = posixpath.normpath(c)
            if nrm.startswith(".."):
                continue
            rel = pkg_dir + nrm
            if rel in by_rel:
                main_info.update({"entry": rel, "exists": True, "location": "asar" if "!/" in rel else "disk"})
                mf = by_rel[rel]
                txt = scan.read(mf, 256 * 1024)
                main_info["browser_window"] = b"BrowserWindow" in txt
                main_info["load_file"] = [x.decode("utf-8", "replace") for x in _LOADFILE_RE.findall(txt)][:10]
                main_info["load_url"] = [x.decode("utf-8", "replace") for x in _LOADURL_RE.findall(txt)][:10]
                main_info["preload"] = [x.decode("utf-8", "replace") for x in _PRELOAD_RE.findall(txt)][:10]
                if main_info["browser_window"]:
                    signals.append("main entry creates a BrowserWindow")
                break
        else:
            main_info["entry"] = pkg_dir + posixpath.normpath(main)
    strong = any(s.startswith(("package.json depends on electron", "Electron runtime files")) for s in signals) or \
        (scan.asar_reports and main_info["browser_window"])
    electron = {"detected": bool(signals), "confidence": "high" if strong else "medium" if signals else "none", "signals": signals,
                "electron_version_hint": alldeps.get("electron"), "version_hint_note": "from package.json dependency range, not verified against the runtime",
                "main": main_info, "primary_package": primary}

    # --- scans over js/html/css ---------------------------------------------------------------------------------
    js_files = sorted((f for f in scan.files if Path(f.rel.split("!/")[-1]).suffix.lower() in JS_EXT), key=lambda f: f.rel)
    css_files = [f for f in scan.files if f.rel.lower().endswith(".css")]
    html_files = [f for f in scan.files if f.rel.lower().endswith((".html", ".htm"))]
    scanned = js_files[:max_scan_files]
    if len(js_files) > max_scan_files:
        scan.truncation.append(f"{len(js_files)} JS files; only the first {max_scan_files} scanned for fingerprints/maps")
    bundlers: dict[str, dict[str, Any]] = {}
    sw_files: list[dict[str, Any]] = []
    sw_regs: list[dict[str, str]] = []
    map_refs: list[dict[str, Any]] = []
    map_records: list[dict[str, Any]] = []
    map_budget = [MAP_READ_BUDGET]
    parsed_by_rel: dict[str, dict[str, Any]] = {}

    def load_map(rel: str, vf: _VF | None, raw: bytes | None, inline: bool) -> dict[str, Any] | None:
        if rel in parsed_by_rel:
            return parsed_by_rel[rel]
        if raw is None and vf is not None:
            if vf.size > max_map_bytes:
                parsed_by_rel[rel] = {"rel": rel, "valid": False, "error": f"map is {vf.size} bytes, over the {max_map_bytes} bound", "inline": inline}
                return parsed_by_rel[rel]
            if vf.size > map_budget[0]:
                parsed_by_rel[rel] = {"rel": rel, "valid": False, "error": "map read budget exhausted", "inline": inline}
                scan.truncation.append("source map read budget exhausted")
                return parsed_by_rel[rel]
            raw = scan.read(vf, vf.size)
            map_budget[0] -= len(raw)
        elif raw is not None:
            if len(raw) > max_map_bytes:
                parsed_by_rel[rel] = {"rel": rel, "valid": False, "error": "inline map over the size bound", "inline": inline}
                return parsed_by_rel[rel]
        m = _parse_map(raw or b"")
        if m is None:
            parsed_by_rel[rel] = {"rel": rel, "valid": False, "error": "not a valid JSON source map", "inline": inline}
            return parsed_by_rel[rel]
        rec = {"rel": rel, "inline": inline, **_map_summary(m), "_map": m}
        parsed_by_rel[rel] = rec
        map_records.append(rec)
        return rec

    for f in scanned:
        head = scan.read(f, HEAD_BYTES)
        tail = scan.read(f, TAIL_BYTES, max(0, f.size - TAIL_BYTES)) if f.size > HEAD_BYTES else head[-TAIL_BYTES:]
        for b, pats in BUNDLER_MARKERS.items():
            hit = [n for n, rx in pats if rx.search(head) or rx.search(tail)]
            if hit:
                e = bundlers.setdefault(b, {"files": 0, "markers": set(), "example_files": []})
                e["files"] += 1
                e["markers"].update(hit)
                if len(e["example_files"]) < 5:
                    e["example_files"].append(f.rel)
        leaf = f.rel.rsplit("/", 1)[-1].lower()
        ev = _SW_EVENT_RE.search(head)
        if leaf in ("sw.js", "service-worker.js", "serviceworker.js", "ngsw-worker.js") or leaf.startswith("workbox-") or (ev and _SW_HINT_RE.search(head)):
            sw_files.append({"path": f.rel, "name_match": leaf in ("sw.js", "service-worker.js", "serviceworker.js", "ngsw-worker.js") or leaf.startswith("workbox-"),
                             "events": sorted({x.decode() for x in _SW_EVENT_RE.findall(head)}), "workbox": b"workbox" in head})
        for url in _SW_REG_RE.findall(head):
            if len(sw_regs) < 100:
                sw_regs.append({"from": f.rel, "script": url.decode("utf-8", "replace")})
        mm = None
        for mm in _SMAP_JS_RE.finditer(tail):
            pass
        if mm is not None:
            url = mm.group(1).decode("utf-8", "replace")
            kind, resolved = _resolve_map_url(f.rel, url)
            ref: dict[str, Any] = {"bundle": f.rel, "kind": kind, "url": url[:200] if kind != "inline" else "data:...", "map": resolved,
                                   "resolved": False}
            if kind == "inline":
                raw = _decode_inline_map(url)
                if raw is not None:
                    rec = load_map(f"{f.rel}#inline", None, raw, True)
                    ref["resolved"] = bool(rec and rec.get("valid"))
                    ref["map"] = f"{f.rel}#inline"
            elif kind == "file" and resolved in by_rel:
                rec = load_map(resolved, by_rel[resolved], None, False)
                ref["resolved"] = bool(rec and rec.get("valid"))
            map_refs.append(ref)
    for f in css_files[:max_scan_files]:
        tail = scan.read(f, TAIL_BYTES, max(0, f.size - TAIL_BYTES))
        mm = None
        for mm in _SMAP_CSS_RE.finditer(tail):
            pass
        if mm is not None:
            url = mm.group(1).decode("utf-8", "replace")
            kind, resolved = _resolve_map_url(f.rel, url)
            ref = {"bundle": f.rel, "kind": kind, "url": url[:200] if kind != "inline" else "data:...", "map": resolved, "resolved": False}
            if kind == "file" and resolved in by_rel:
                rec = load_map(resolved, by_rel[resolved], None, False)
                ref["resolved"] = bool(rec and rec.get("valid"))
            map_refs.append(ref)
    # standalone .map files (referenced or not)
    map_files = sorted((f for f in scan.files if f.rel.lower().endswith(".map")), key=lambda f: f.rel)
    for f in map_files[:MAX_MAPS]:
        load_map(f.rel, f, None, False)
    if len(map_files) > MAX_MAPS:
        scan.truncation.append(f"{len(map_files)} .map files; only the first {MAX_MAPS} parsed")
    referenced = {r["map"] for r in map_refs if r["map"]}
    for rec in parsed_by_rel.values():
        rec["referenced"] = rec["rel"] in referenced or rec.get("inline", False)

    # --- html & manifest ----------------------------------------------------------------------------------------
    html_entries = []
    manifest_links: list[str] = []
    for f in sorted(html_files, key=lambda f: f.rel)[:200]:
        head = scan.read(f, HEAD_BYTES)
        scripts = [x.decode("utf-8", "replace") for x in _SCRIPT_SRC_RE.findall(head)][:20]
        for a, b in _LINK_MANIFEST_RE.findall(head):
            manifest_links.append((a or b).decode("utf-8", "replace"))
        for url in _SW_REG_RE.findall(head):
            sw_regs.append({"from": f.rel, "script": url.decode("utf-8", "replace")})
        html_entries.append({"path": f.rel, "scripts": scripts})
    manifests = []
    for f in sorted(scan.files, key=lambda f: f.rel):
        leaf = f.rel.rsplit("/", 1)[-1].lower()
        if not (leaf.endswith(".webmanifest") or leaf == "manifest.json"):
            continue
        mj = _json_file(scan, f, 1 << 20)
        if not isinstance(mj, dict):
            continue
        looks = leaf.endswith(".webmanifest") or (("name" in mj or "short_name" in mj) and ("start_url" in mj or "display" in mj or "icons" in mj))
        if looks:
            manifests.append({"path": f.rel, "name": mj.get("name"), "short_name": mj.get("short_name"), "start_url": mj.get("start_url"),
                              "display": mj.get("display"), "scope": mj.get("scope"),
                              "icons": len(mj.get("icons", [])) if isinstance(mj.get("icons"), list) else 0})
    bundler_out = {b: {"files": e["files"], "markers": sorted(e["markers"]), "strength": "strong" if len(e["markers"]) >= 2 else "weak",
                       "example_files": e["example_files"]} for b, e in sorted(bundlers.items())}
    maps_valid = [r for r in parsed_by_rel.values() if r.get("valid")]
    bundles_with_ref = {r["bundle"] for r in map_refs}
    maps_out = []
    for r in sorted(parsed_by_rel.values(), key=lambda r: r["rel"]):
        maps_out.append({k: v for k, v in r.items() if k != "_map"})
    report = {
        "schema": SCHEMA_VERSION,
        "counts": {"files": len(scan.files), "disk_files": scan.disk_files, "total_bytes": total_bytes, "by_extension": dict(sorted(exts.items(), key=lambda kv: -kv[1])[:30]),
                   "js_files": len(js_files), "js_files_scanned": len(scanned), "html_files": len(html_files), "css_files": len(css_files),
                   "symlinks_not_followed": scan.symlinks},
        "node_modules": {"directories": scan.node_modules["dirs"], "top_level_packages": cap_list(scan.node_modules["packages"], 200),
                         "note": "contents of node_modules are not inventoried"},
        "package_json": {"primary": primary, "all": cap_list(packages, 100)},
        "electron": electron,
        "asar": [{k: v for k, v in a.items() if k != "reader"} for a in scan.asar_reports],
        "bundlers": {"detected": bundler_out, "note": "heuristic string markers in the first 64KB / last 4KB of each JS file; minification or re-bundling can hide or imitate them"},
        "source_maps": {"maps_found": len(parsed_by_rel), "maps_valid": len(maps_valid),
                        "maps_with_sources_content": sum(1 for r in maps_valid if r.get("sources_with_content")),
                        "sources_with_content_total": sum(r.get("sources_with_content", 0) for r in maps_valid),
                        "sources_without_content_total": sum(r.get("sources_without_content", 0) for r in maps_valid),
                        "bundle_references": cap_list(map_refs, 500),
                        "js_files_scanned_without_map_reference": sum(1 for f in scanned if f.rel not in bundles_with_ref),
                        "maps": cap_list(maps_out, 500)},
        "service_worker": {"detected": bool(sw_files), "files": cap_list(sw_files, 50), "registrations": cap_list(sw_regs, 50)},
        "manifest": {"detected": bool(manifests), "files": cap_list(manifests, 20), "linked_from_html": sorted(set(manifest_links))[:20]},
        "html_entries": cap_list(html_entries, 100),
        "files": cap_list([{"path": f.rel, "size": f.size} for f in sorted(scan.files, key=lambda f: f.rel)], LIST_CAP),
    }
    return report, map_records


def _make_scan(root: Path, limits: ArchiveLimits, max_files: int) -> _Scan:
    if root.is_file():
        scan = _Scan(root=root.parent, root_kind="file")
        sz = root.stat().st_size
        scan.files.append(_VF(rel=root.name, size=sz, path=root))
        scan.disk_files = 1
        scan.disk_total_bytes = sz
        if root.name.lower().endswith(".asar"):
            scan.root_kind = "asar"
            scan.asar_paths.append((root.name, root))
        return scan
    scan = _Scan(root=root, root_kind="dir")
    _walk(root, scan, limits, max_files)
    return scan


_PROBE_CACHE: dict[tuple[str, float], str | None] = {}


class JSWebBackend(BackendAdapter):
    backend_id = BACKEND_ID

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # -- discovery -----------------------------------------------------------------------------------------------
    def _optional_probe(self, name: str, subdirs: list[str], flag: str, pinned: str, source: str, license_: str, integrity: str,
                        hint: str) -> RecoveryToolProbe:
        exe = discover_executable(self.settings, [name], subdirs=subdirs)
        if exe is None:
            return RecoveryToolProbe(name, Availability.MISSING, detail="optional; not found", pinned=pinned, source=source, license=license_,
                                     next_action=hint, prerequisites=["Node.js"] if name == "asar" else [], optional=True)
        try:
            key = (str(exe), exe.stat().st_mtime)
        except OSError:
            key = (str(exe), 0.0)
        if key not in _PROBE_CACHE:
            ver = None
            try:
                r = run_bounded(None, [str(exe), flag], limits=self.settings.limits, timeout=30)
                m = re.search(r"v?(\d+\.\d+\.\d+\S*)", r.text)
                ver = m.group(1) if m else None
            except (StageError, OSError):
                ver = None
            _PROBE_CACHE[key] = ver
        ver = _PROBE_CACHE[key]
        if ver is None:
            return RecoveryToolProbe(name, Availability.DETECTED, path=str(exe), detail=f"found but {flag} failed", pinned=pinned, source=source,
                                     license=license_, next_action=hint, optional=True)
        return RecoveryToolProbe(name, Availability.INSTALLED, path=str(exe), version=ver, pinned=pinned, source=source, license=license_,
                                 integrity=integrity if ver == pinned else "",
                                 detail="" if ver == pinned or not pinned else f"version differs from pinned {pinned}", optional=True)

    def probe(self) -> BackendInfo:
        native = RecoveryToolProbe(ENGINE_NAME, Availability.INSTALLED, version=ENGINE_VERSION, license="project (built-in, pure Python)",
                                   source="built-in", detail="native asar/source-map/bundler inspection; no external tool required",
                                   integration="library")
        node = self._optional_probe("node", ["node", "nodejs", "node/bin"], "--version", "", "https://nodejs.org", "MIT", "", NODE_INSTALL_HINT)
        asar = self._optional_probe("asar", ["asar"], "--version", ASAR_PINNED, "https://github.com/electron/asar", "MIT", ASAR_INTEGRITY, ASAR_INSTALL_HINT)
        return BackendInfo(
            backend_id=BACKEND_ID, title="JS / web / Electron inspection",
            formats=["js_bundle", "electron_asar", "web_app"], platforms=["linux", "windows", "macos"],
            profiles=["js_web", "electron"],
            operations=[
                Operation("inspect", "Find package.json, Electron main, asar, source maps, bundlers, service worker, manifest", {"root": "path"}, {"inspection": "dict"}),
                Operation("extract", "Bounded extraction of loose files, asar contents and source-map sourcesContent", {"root": "path", "out_dir": "path"}, {"extraction_report": "dict"}),
            ],
            tools=[native, node, asar], resources={"ram_mb": 200, "next_action": ""},
            experimental=False)

    def smoke(self) -> ToolProbe:
        tool = self.probe().tools[0]
        smap = json.dumps({"version": 3, "sources": ["src/a.js"], "sourcesContent": ["export const a = 1;\n"], "names": [], "mappings": "AAAA"})
        files = {"package.json": json.dumps({"name": "smoke", "main": "main.js", "devDependencies": {"electron": "1.0.0"}}).encode(),
                 "main.js": b"const {BrowserWindow}=require('electron');\n", "bundle.js": b"var __webpack_require__={};\n//# sourceMappingURL=bundle.js.map\n",
                 "bundle.js.map": smap.encode()}
        with tempfile.TemporaryDirectory(prefix="rs-jsweb-smoke-") as td:
            root = Path(td) / "app"
            (root / "resources").mkdir(parents=True)
            (root / "resources" / "app.asar").write_bytes(build_asar(files))
            r = self.inspect(root)
            ok = (r.ok and r.data["electron"]["detected"] and "webpack" in r.data["bundlers"]["detected"]
                  and r.data["source_maps"]["sources_with_content_total"] == 1)
            if ok:
                out = Path(td) / "out"
                e = self.extract(root, out)
                ok = e.ok and any(out.rglob("a.js"))
        tool.availability = Availability.USABLE if ok else Availability.INSTALLED
        tool.detail = "inspected and extracted a built-in asar + source map sample" if ok else "smoke failed"
        return tool

    # -- operations ----------------------------------------------------------------------------------------------
    def op_inspect(self, ctx: Any, root: str, **kw: Any) -> OperationResult:
        return self.inspect(root, ctx=ctx, **kw)

    def op_extract(self, ctx: Any, root: str, out_dir: str, **kw: Any) -> OperationResult:
        return self.extract(root, out_dir, ctx=ctx, **kw)

    def _limits(self) -> ArchiveLimits:
        return ArchiveLimits.coerce(self.settings.limits)

    def _fingerprint(self, root: Path, scan: _Scan, module_sha256: str | None) -> tuple[str, bool]:
        if module_sha256:
            return module_sha256, False
        if root.is_file():
            return sha256_file(root), False
        return tree_digest([(f.rel, f.path) for f in scan.files if f.path is not None])

    def inspect(self, root: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None, module_id: str | None = None,
                module_sha256: str | None = None, max_scan_files: int = MAX_SCAN_FILES, max_map_bytes: int = MAX_MAP_BYTES) -> OperationResult:
        rp = Path(root)
        if not rp.exists():
            return OperationResult(ok=False, error=f"not found: {rp}")
        limits = self._limits()
        scan = _make_scan(rp, limits, self.settings.limits.max_inventory_files)
        try:
            _load_asars(scan, limits)
            report, _maps = _analyse(scan, limits, max_scan_files=max_scan_files, max_map_bytes=max_map_bytes)
            fp, partial = self._fingerprint(rp, scan, module_sha256)
        finally:
            scan.close()
        report.update({"root": str(rp), "root_kind": scan.root_kind, "tree_sha256": fp, "tree_fingerprint_partial": partial,
                       "truncated": bool(scan.truncation), "truncation": scan.truncation, "warnings": scan.warnings[:50],
                       "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION},
                       "claims": "Structure and markers only. No code is reconstructed or deobfuscated by inspection."})
        inputs = {"op": "inspect", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": ENGINE_NAME, "tool_version": ENGINE_VERSION,
                  "module_sha256": fp, "max_scan_files": max_scan_files, "max_map_bytes": max_map_bytes}
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "js.inspection", f"JS/web inspection: {rp.name}", report, module_id=module_id,
                              inputs=inputs, producer=BACKEND_ID)
        trunc = report["truncated"] or any(isinstance(v, dict) and v.get("truncated") for v in (report["files"], report["source_maps"]["maps"]))
        return OperationResult(ok=True, data=report, evidence_ids=[eid] if eid else [], truncated=bool(trunc))

    def extract(self, root: Path | str, out_dir: Path | str, *, ctx: Any = None, studio: Any = None, case_id: str | None = None,
                module_id: str | None = None, module_sha256: str | None = None, source_root: Path | str | None = None,
                copy_all_files: bool = False, skip_third_party_sources: bool = False,
                max_scan_files: int = MAX_SCAN_FILES, max_map_bytes: int = MAX_MAP_BYTES) -> OperationResult:
        rp, out = Path(root), Path(out_dir)
        if not rp.exists():
            return OperationResult(ok=False, error=f"not found: {rp}")
        try:
            assert_output_not_in_source(out, source_root if source_root is not None else rp)
            if is_within(resolve_final(rp), resolve_final(out)):
                raise PathPolicyError(f"output directory {out} contains the input {rp}")
        except PathPolicyError as e:
            return OperationResult(ok=False, error=f"path policy: {e}")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            return OperationResult(ok=False, error=f"output directory {out} exists and is not empty; refusing to mix outputs")
        limits = self._limits()
        scan = _make_scan(rp, limits, self.settings.limits.max_inventory_files)
        rep = ExtractionReport("jsweb", str(rp), str(out))
        try:
            _load_asars(scan, limits)
            report, maps = _analyse(scan, limits, max_scan_files=max_scan_files, max_map_bytes=max_map_bytes)
            fp, partial = self._fingerprint(rp, scan, module_sha256)
            out.mkdir(parents=True, exist_ok=True)
            sink = SafeWriter(out, limits, rep)
            # ---- 1. asar archives ------------------------------------------------------------------------------
            asar_out: list[dict[str, Any]] = []
            copied, filtered = 0, {}
            if not rep.truncated:
                for rel, p in scan.asar_paths[:MAX_ASARS]:
                    left_b = limits.max_expansion_bytes - rep.bytes_written
                    left_e = limits.max_entries - rep.files_extracted - rep.dirs_created
                    if left_b <= 0 or left_e <= 0:
                        rep.truncated, rep.truncation_reason = True, "budget exhausted before all asar archives were extracted"
                        break
                    sub = "asar/" + re.sub(r"\.asar$", "", rel).replace("/", "__")
                    try:
                        r = extract_archive(p, out / sub, limits=ArchiveLimits(max_entries=left_e, max_expansion_bytes=left_b), format="asar")
                    except ArchiveError as e:
                        asar_out.append({"archive": rel, "ok": False, "error": str(e)})
                        rep.error(f"{rel}: {e}")
                        continue
                    rep.files_extracted += r.files_extracted
                    rep.dirs_created += r.dirs_created
                    rep.bytes_written += r.bytes_written
                    rep.skipped_total += r.skipped_total
                    rep.refused_total += r.refused_total
                    for x in r.refused:
                        if len(rep.refused) < 200:
                            rep.refused.append({**x, "name": f"{rel}!/{x['name']}"})
                    for x in r.skipped:
                        if len(rep.skipped) < 200:
                            rep.skipped.append({**x, "name": f"{rel}!/{x['name']}"})
                    if r.truncated:
                        rep.truncated, rep.truncation_reason = True, f"{rel}: {r.truncation_reason}"
                    asar_out.append({"archive": rel, "ok": True, "out": sub, "files": r.files_extracted, "dirs": r.dirs_created, "bytes": r.bytes_written,
                                     "entries_seen": r.entries_seen, "declared_entries": r.declared_entries, "truncated": r.truncated,
                                     "refused": r.refused_total, "skipped": r.skipped_total, "errors": r.errors[:10]})
            # ---- 2. sourcesContent -----------------------------------------------------------------------------
            recovered: list[dict[str, Any]] = []
            no_content: list[dict[str, str]] = []
            third_party_skipped = 0
            sanitized_count = 0
            for mi, rec in enumerate(sorted(maps, key=lambda r: r["rel"])):
                m = rec["_map"]
                if m.get("_indexed"):
                    continue
                srcs = m.get("sources") if isinstance(m.get("sources"), list) else []
                cont = m.get("sourcesContent") if isinstance(m.get("sourcesContent"), list) else []
                mdir = f"sources/m{mi:03d}-" + re.sub(r"[^A-Za-z0-9._-]", "_", rec["rel"].rsplit("/", 1)[-1])[:60]
                seen: set[str] = set()
                for si, sname in enumerate(srcs):
                    if not isinstance(sname, str):
                        continue
                    c = cont[si] if si < len(cont) else None
                    if not isinstance(c, str):
                        if len(no_content) < 500:
                            no_content.append({"map": rec["rel"], "source": sname[:200]})
                        continue
                    if skip_third_party_sources and "node_modules/" in sname:
                        third_party_skipped += 1
                        continue
                    rel_path, changed = sanitize_source_path(sname, si)
                    if rel_path in seen:
                        stem, dot, ext = rel_path.rpartition(".")
                        k = 2
                        while (f"{stem}~{k}.{ext}" if dot else f"{rel_path}~{k}") in seen:
                            k += 1
                        rel_path = f"{stem}~{k}.{ext}" if dot else f"{rel_path}~{k}"
                    seen.add(rel_path)
                    data = c.encode("utf-8")
                    if rep.files_extracted >= limits.max_entries or sink.would_exceed(len(data)):
                        rep.truncated, rep.truncation_reason = True, "budget exhausted while writing source-map sources"
                        break
                    if sink.write(f"{mdir}/{rel_path}", iter([data]), len(data)):
                        sanitized_count += 1 if changed else 0
                        recovered.append({"map": rec["rel"], "source": sname[:300], "path": f"{mdir}/{rel_path}", "bytes": len(data),
                                          "sha256": hashlib.sha256(data).hexdigest(), "neutralized_escape": changed,
                                          "third_party": "node_modules/" in sname,
                                          "provenance": "sourcemap.sourcesContent"})
                if rep.truncated:
                    break
            # ---- 3. loose web files (last: least valuable, may be large) ----------------------------------------------------------------------------
            for f in sorted(scan.files, key=lambda f: f.rel):
                if f.path is None:
                    continue
                ext = Path(f.rel).suffix.lower()
                if not copy_all_files and ext not in WEB_EXT:
                    filtered[ext or "(none)"] = filtered.get(ext or "(none)", 0) + 1
                    continue
                if f.rel.lower().endswith(".asar"):
                    continue
                if rep.files_extracted >= limits.max_entries:
                    rep.truncated, rep.truncation_reason = True, f"entry limit {limits.max_entries} reached"
                    break
                if sink.would_exceed(f.size):
                    rep.truncated, rep.truncation_reason = True, f"expansion limit {limits.max_expansion_bytes} bytes reached before {f.rel!r}"
                    break
                with open(f.path, "rb") as fh:
                    if sink.write(f"tree/{f.rel}", read_chunks(fh, f.size), f.size):
                        copied += 1
                if sink.budget_hit:
                    rep.truncated, rep.truncation_reason = True, f"expansion limit reached while writing {f.rel!r}"
                    break
            manifest = {
                "schema": SCHEMA_VERSION, "root": str(rp), "tree_sha256": fp,
                "layout": {"tree/": "loose web files copied from the input (provenance: original_file)",
                           "asar/<name>/": "asar contents (provenance: asar_member), including .unpacked siblings",
                           "sources/<map>/": "sourcesContent embedded in source maps (provenance: sourcemap.sourcesContent = original author source)"},
                "tree": {"copied": copied, "filtered_out_by_extension": filtered, "node_modules_skipped": scan.node_modules["dirs"] > 0},
                "asar": asar_out, "sources_recovered": cap_list(recovered, LIST_CAP), "sources_recovered_count": len(recovered),
                "sources_known_without_content": cap_list(no_content, 500),
            }
            body = json.dumps(manifest, indent=1).encode("utf-8")
            sink.write("extraction_manifest.json", iter([body]), len(body))
        finally:
            scan.close()
        summary = {
            "schema": SCHEMA_VERSION, "status": "partial" if (rep.truncated or rep.refused_total or rep.errors or no_content) else "ok",
            "root": str(rp), "out_dir": str(out), "tree_sha256": fp, "tree_fingerprint_partial": partial,
            "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION},
            "written": {"files": rep.files_extracted, "dirs": rep.dirs_created, "bytes": rep.bytes_written},
            "loose_files_copied": copied, "loose_files_filtered": filtered, "asar": asar_out,
            "sources_from_source_maps": {"recovered": len(recovered), "neutralized_paths": sanitized_count, "third_party_skipped": third_party_skipped,
                                         "known_names_without_content": len(no_content), "provenance": "sourcemap.sourcesContent",
                                         "items": cap_list(recovered, LIST_CAP)},
            "refused": rep.refused, "refused_total": rep.refused_total, "skipped": rep.skipped, "skipped_total": rep.skipped_total,
            "errors": rep.errors, "truncated": rep.truncated or bool(scan.truncation), "truncation_reason": rep.truncation_reason,
            "inspection_truncation": scan.truncation,
            "limits": {"max_entries": limits.max_entries, "max_expansion_bytes": limits.max_expansion_bytes},
            "claims": "Files are copied or extracted verbatim. Only sourcesContent recovered from source maps is original author source; "
                      "minified bundles without source maps stay minified. No equivalence with the original project is asserted.",
        }
        inputs = {"op": "extract", "backend": BACKEND_ID, "schema": SCHEMA_VERSION, "tool": ENGINE_NAME, "tool_version": ENGINE_VERSION,
                  "module_sha256": fp, "copy_all_files": copy_all_files,
                  "skip_third_party_sources": skip_third_party_sources,
                  "limits": [limits.max_entries, limits.max_expansion_bytes]}
        eid = record_evidence(resolve_studio(ctx, studio), case_id, "js.extraction_report", f"JS/web extraction: {rp.name}", summary, module_id=module_id,
                              inputs=inputs, producer=BACKEND_ID)
        return OperationResult(ok=True, data={"extraction_report": summary, "out_dir": str(out)}, evidence_ids=[eid] if eid else [],
                               truncated=bool(summary["truncated"]))
