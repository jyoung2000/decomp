"""Complete folder inventory with explicit disclosure of skipped/unsupported/inaccessible/truncated material.

Never follows links outside the root. Counts files and bytes; does not claim feature coverage.
"""
from __future__ import annotations

import os
import stat as statmod
from pathlib import Path
from typing import Any, Callable

from ..config import Limits
from ..ids import sha256_file
from ..paths import classify_entry, is_within, resolve_final
from .detect import Detection, embedded_pck_offset, sniff, summarize_profile

MODULE_FORMATS = {"pe", "elf", "macho", "dotnet", "godot_pck", "asar", "wasm", "js_bundle",
                  "jar", "apk", "aab", "dex", "gamemaker_data", "unreal_pak", "unreal_iostore", "il2cpp_metadata"}
SKIP_NAMES = {"$recycle.bin", "system volume information"}


def inventory_root(root: Path, limits: Limits, *, progress: Callable[[dict[str, Any]], None] | None = None,
                   hash_max_bytes: int = 2 * 1024 * 1024 * 1024) -> dict[str, Any]:
    root = resolve_final(root)
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    dirs = 0
    total_bytes = 0
    truncated = False
    detections: list[tuple[str, Detection]] = []
    seen_inodes: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda e: skipped.append({"path": str(getattr(e, 'filename', '')), "reason": f"inaccessible: {e.strerror}"})):
        dp = Path(dirpath)
        # Prune links/junctions among directories without following them.
        keep = []
        for d in dirnames:
            full = dp / d
            kind = classify_entry(full)
            if kind in ("symlink", "junction", "reparse"):
                links.append({"path": _rel(full, root), "kind": kind, "target": _readlink(full), "followed": False})
            elif d.lower() in SKIP_NAMES:
                skipped.append({"path": _rel(full, root), "reason": "system folder"})
            else:
                keep.append(d)
        dirnames[:] = sorted(keep)
        dirs += 1
        for fn in sorted(filenames):
            if len(files) >= limits.max_inventory_files:
                truncated = True
                break
            full = dp / fn
            kind = classify_entry(full)
            rel = _rel(full, root)
            if kind in ("symlink", "junction", "reparse"):
                tgt = _readlink(full)
                inside = False
                try:
                    inside = is_within(resolve_final(full), root)
                except Exception:
                    pass
                links.append({"path": rel, "kind": kind, "target": tgt, "followed": False, "inside_root": inside})
                continue
            if kind == "other":
                skipped.append({"path": rel, "reason": "unsupported entry type"})
                continue
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError as e:
                skipped.append({"path": rel, "reason": f"inaccessible: {e.strerror}"})
                continue
            if not statmod.S_ISREG(st.st_mode):
                skipped.append({"path": rel, "reason": "not a regular file"})
                continue
            entry: dict[str, Any] = {"path": rel, "size": st.st_size, "mtime": int(st.st_mtime), "kind": kind}
            key = (st.st_dev, st.st_ino)
            if kind == "hardlink":
                entry["hardlink_group"] = f"{st.st_dev}:{st.st_ino}"
                entry["hardlink_duplicate"] = key in seen_inodes
            seen_inodes.add(key)
            total_bytes += st.st_size
            try:
                with open(full, "rb") as f:
                    head = f.read(0x1000)
            except OSError as e:
                entry["error"] = f"unreadable: {e.strerror}"
                skipped.append({"path": rel, "reason": f"unreadable: {e.strerror}"})
                files.append(entry)
                continue
            det = sniff(full, head)
            entry["detect"] = det.to_dict()
            if st.st_size <= hash_max_bytes:
                try:
                    entry["sha256"] = sha256_file(full)
                except OSError as e:
                    entry["error"] = f"hash failed: {e.strerror}"
            else:
                entry["sha256"] = None
                skipped.append({"path": rel, "reason": f"not hashed: larger than {hash_max_bytes} bytes"})
            if det.format == "pe" and det.profile == "native_pe":
                off = embedded_pck_offset(full)
                if off is not None:
                    entry["embedded_pck_offset"] = off
                    det.flags["embedded_pck"] = True
                    detections.append((rel, Detection("godot_pck", "godot")))
            if det.flags.get("encrypted"):
                skipped.append({"path": rel, "reason": "encrypted content"})
            detections.append((rel, det))
            files.append(entry)
            if progress and len(files) % 500 == 0:
                progress({"files_scanned": len(files), "bytes": total_bytes, "truncated": truncated})
        if truncated:
            break
    modules = [f for f in files if f.get("detect", {}).get("format") in MODULE_FORMATS]
    profile = summarize_profile(detections, root)
    return {
        "root": str(root), "files": files, "file_count": len(files), "dir_count": dirs, "total_bytes": total_bytes,
        "modules": [m["path"] for m in modules], "module_count": len(modules), "links": links, "skipped": skipped,
        "truncated": truncated, "truncation_limit": limits.max_inventory_files if truncated else None,
        "profile": profile, "unknown_scope": _unknown_scope(files, skipped, links, truncated),
    }


def _unknown_scope(files, skipped, links, truncated) -> list[str]:
    out = []
    if truncated:
        out.append("inventory truncated: file count exceeded the configured limit; unscanned files are unknown scope")
    if skipped:
        out.append(f"{len(skipped)} entries skipped (inaccessible/unsupported/unhashed); their contents are unknown scope")
    if links:
        out.append(f"{len(links)} links/junctions recorded but not followed")
    enc = [f for f in files if f.get("detect", {}).get("flags", {}).get("encrypted")]
    if enc:
        out.append(f"{len(enc)} encrypted files cannot be analysed")
    return out


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _readlink(p: Path) -> str | None:
    try:
        return os.readlink(p)
    except OSError:
        return None


def build_dependency_graph(inv: dict[str, Any]) -> dict[str, Any]:
    """Module → imported module edges from PE import tables (and package.json deps for web)."""
    by_name = {f["path"].rsplit("/", 1)[-1].lower(): f["path"] for f in inv["files"]}
    edges: list[dict[str, Any]] = []
    external: dict[str, list[str]] = {}
    for f in inv["files"]:
        det = f.get("detect", {})
        for imp in det.get("flags", {}).get("imports", []) or []:
            if imp in by_name:
                edges.append({"from": f["path"], "to": by_name[imp], "kind": "import"})
            else:
                external.setdefault(f["path"], []).append(imp)
    return {"edges": edges, "external": external, "nodes": inv["modules"]}
