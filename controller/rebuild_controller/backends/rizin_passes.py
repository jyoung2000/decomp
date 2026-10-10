"""Post-analysis passes run inside a RizinSession after ``aaa`` (R1: "no-AI native analysis depth").

Each pass is a general mechanism, not knowledge about a particular binary:

* ``pdata``   - x64 PE exception directory (RUNTIME_FUNCTION): every non-chained entry is a function start the OS unwinder
                relies on. Entries that are EH funclets (cleanup/catch pads that run on the parent's frame: they begin by
                spilling rdx and deriving rbp from it) are recorded as funclets, not functions. Missing starts are defined
                with ``af``.
* ``thunks``  - import thunks (a function whose only instruction is ``jmp [IAT slot]``) are named after the import
                (``strchr``), the way IDA/Ghidra name them; rizin's default is ``sub.<dll>_<name>``.
* ``sigpacks``- FLIRT signature packs shipped with Rebuild Studio (``data/sigpacks/<format>/<arch>/<bits>/*.sig``, pinned by
                sha256 in ``data/sigpacks/manifest.json``) are applied with ``Fs`` for compiler runtimes (MSVC CRT, Rust std)
                on top of rizin's own sigdb.

Passes only add functions or rename auto-named ones (``fcn.*``, ``sub.*``); they never rename a function that already
has a symbol or a user annotation (annotations are replayed after the passes anyway).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

DATA = Path(__file__).resolve().parents[1] / "data"
SIGPACKS = DATA / "sigpacks"
AUTO_NAME = re.compile(r"^(fcn\.|sub\.|entry\d*$|loc\.)")
DEFAULT_PASSES = ("sigpacks", "pdata", "relocptrs", "thunks")
MAX_NEW_FUNCTIONS = 20_000


def _addr(a: int) -> str:
    return f"0x{int(a):x}"


def is_funclet_prologue(code: bytes) -> bool:
    """MSVC/LLVM Windows EH funclet: ``mov [rsp+0x10], rdx`` first, then rbp derived from rdx (``mov rbp, rdx`` or
    ``lea rbp, [rdx+disp]``) within the prologue. Ordinary functions do not take their frame pointer from rdx."""
    if not code.startswith(b"\x48\x89\x54\x24\x10"):
        return False
    head = code[:48]
    return (b"\x48\x8b\xea" in head or b"\x48\x89\xd5" in head          # mov rbp, rdx
            or b"\x48\x8d\x6a" in head or b"\x48\x8d\xaa" in head)       # lea rbp, [rdx+disp8/disp32]


def pdata_starts(path: Path) -> dict[str, Any]:
    """Function starts from a PE32+ exception directory: {"starts": [va...], "funclets": [va...], "chained": n}."""
    out: dict[str, Any] = {"starts": [], "funclets": [], "chained": 0, "entries": 0}
    try:
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
    except Exception as e:  # not a PE / malformed: nothing to add
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out
    try:
        if pe.FILE_HEADER.Machine != 0x8664:
            return out
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXCEPTION"]])
        base = pe.OPTIONAL_HEADER.ImageBase
        for e in getattr(pe, "DIRECTORY_ENTRY_EXCEPTION", []) or []:
            out["entries"] += 1
            ui = getattr(e, "unwindinfo", None)
            if ui is not None and (ui.Flags & 0x4):   # UNW_FLAG_CHAININFO: a fragment of another function
                out["chained"] += 1
                continue
            rva = e.struct.BeginAddress
            try:
                code = pe.get_data(rva, 48)
            except Exception:
                code = b""
            (out["funclets"] if is_funclet_prologue(code) else out["starts"]).append(base + rva)
        out["image_base"] = base
    finally:
        pe.close()
    return out


def _functions(sess: Any, poll) -> list[dict[str, Any]]:
    text, _ = sess._exec("aflj", timeout=300, poll=poll)
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    return [f for f in data if isinstance(f, dict)]


def pass_pdata(sess: Any, poll) -> dict[str, Any]:
    info = pdata_starts(sess.path)
    if not info["starts"]:
        return {k: v for k, v in info.items() if k != "starts" and k != "funclets"} | {"added": 0}
    rz_base = _baddr(sess, poll)
    delta = (rz_base - info["image_base"]) if rz_base is not None and "image_base" in info else 0
    have = {f.get("offset") for f in _functions(sess, poll)}
    added = 0
    for va in sorted(info["starts"]):
        va += delta
        if va in have or added >= MAX_NEW_FUNCTIONS:
            continue
        sess._exec(f"af @ {_addr(va)}", timeout=60, poll=poll)
        added += 1
    return {"entries": info["entries"], "chained": info["chained"], "funclets": len(info["funclets"]),
            "starts": len(info["starts"]), "added": added}


def _baddr(sess: Any, poll) -> int | None:
    text, _ = sess._exec("iIj", timeout=30, poll=poll)
    try:
        v = json.loads(text or "{}").get("baddr")
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def reloc_pointers(path: Path) -> dict[str, Any]:
    """Every absolute pointer the PE base-relocation table describes: {"pairs": [(slot_va, value)], "image_base"}."""
    out: dict[str, Any] = {"pairs": []}
    try:
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_BASERELOC"]])
        base = pe.OPTIONAL_HEADER.ImageBase
        for block in getattr(pe, "DIRECTORY_ENTRY_BASERELOC", []) or []:
            for r in block.entries:
                if r.type not in (3, 10):
                    continue
                try:
                    v = pe.get_qword_at_rva(r.rva) if r.type == 10 else pe.get_dword_at_rva(r.rva)
                except Exception:
                    continue
                if v is not None:
                    out["pairs"].append((base + r.rva, v))
        out["image_base"] = base
    finally:
        pe.close()
    return out


def reloc_code_pointers(path: Path) -> dict[str, Any]:
    """Absolute pointers into executable sections, found through the PE base-relocation table (vtables, callback tables,
    function-pointer arrays, x86 jump tables). Returns {"targets": [va...], "image_base": int}."""
    out: dict[str, Any] = {"targets": [], "relocs": 0}
    try:
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_BASERELOC"]])
        base = pe.OPTIONAL_HEADER.ImageBase
        code = [(base + s.VirtualAddress, base + s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData))
                for s in pe.sections if s.Characteristics & 0x20000000]
        targets: set[int] = set()
        for block in getattr(pe, "DIRECTORY_ENTRY_BASERELOC", []) or []:
            for r in block.entries:
                if r.type not in (3, 10):            # IMAGE_REL_BASED_HIGHLOW / DIR64
                    continue
                out["relocs"] += 1
                try:
                    v = pe.get_qword_at_rva(r.rva) if r.type == 10 else pe.get_dword_at_rva(r.rva)
                except Exception:
                    continue
                if v is not None and any(a <= v < b for a, b in code):
                    targets.add(v)
        padding = 0
        for v in sorted(targets):          # a pointer into int3/nop/zero padding is a marker, not a function start
            try:
                first = pe.get_data(v - base, 1)
            except Exception:
                first = b""
            if first in (b"\xcc", b"\x90", b"\x00", b""):
                targets.discard(v)
                padding += 1
        out["targets"] = sorted(targets)
        out["padding_skipped"] = padding
        out["image_base"] = base
    finally:
        pe.close()
    return out


def pass_relocptrs(sess: Any, poll) -> dict[str, Any]:
    """Define functions at relocated code pointers that no analysed function covers (callbacks, vtable slots)."""
    info = reloc_code_pointers(sess.path)
    if not info["targets"]:
        return {"relocs": info["relocs"], "code_pointers": 0, "added": 0}
    rz_base = _baddr(sess, poll)
    delta = (rz_base - info["image_base"]) if rz_base is not None and "image_base" in info else 0
    funcs = _functions(sess, poll)
    starts = {f.get("offset") for f in funcs}
    spans = sorted((f.get("minbound", f.get("offset")), f.get("maxbound", f.get("offset"))) for f in funcs
                   if isinstance(f.get("offset"), int))
    import bisect
    lows = [a for a, _ in spans]

    def covered(va: int) -> bool:
        i = bisect.bisect_right(lows, va) - 1
        while i >= 0 and i >= bisect.bisect_right(lows, va) - 64:
            a, b = spans[i]
            if a <= va < b:
                return True
            i -= 1
        return False
    added = 0
    for va in info["targets"]:
        va += delta
        if va in starts or covered(va) or added >= MAX_NEW_FUNCTIONS:
            continue
        sess._exec(f"af @ {_addr(va)}", timeout=60, poll=poll)
        added += 1
    return {"relocs": info["relocs"], "code_pointers": len(info["targets"]), "added": added}


# Names sent to ``afn`` are restricted to the same identifier charset the annotation layer uses: ``@`` is rizin's
# temporary-seek operator and ``?``/``<``/``>``/``|``/``;`` are command syntax, so mangled C++ imports keep rizin's name.
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def pass_thunks(sess: Any, poll) -> dict[str, Any]:
    """Name ``jmp [IAT]`` thunks after their import. rizin names them ``sub.<dll>_<name>`` and the import table gives
    ``<name>``; only auto-named single-jump functions are renamed."""
    text, _ = sess._exec("iij", timeout=60, poll=poll)
    try:
        imports = json.loads(text or "[]")
    except ValueError:
        imports = []
    by_iat = {i.get("plt"): i.get("name") for i in imports if isinstance(i, dict) and i.get("plt") and i.get("name")}
    renamed = 0
    taken = {f.get("name") for f in _functions(sess, poll)}
    for f in _functions(sess, poll):
        name = f.get("name") or ""
        if not name.startswith(("sub.", "fcn.")) or (f.get("size") or 0) > 8 or f.get("nbbs") != 1:
            continue
        dis, _ = sess._exec(f"pdj 1 @ {_addr(f['offset'])}", timeout=30, poll=poll)
        try:
            op = (json.loads(dis or "[]") or [{}])[0]
        except ValueError:
            continue
        target = op.get("ptr")
        imp = by_iat.get(target)
        if not imp or "jmp" not in str(op.get("type") or ""):
            continue
        new = imp.split("_", 1)[1] if "." in imp.split("_", 1)[0] and "_" in imp else imp   # "KERNEL32.dll_X" -> "X"
        if not SAFE_NAME.fullmatch(new):
            continue
        cand = new if new not in taken else f"j_{new}"
        if cand in taken:
            continue
        sess._exec(f"afn {cand} @ {_addr(f['offset'])}", timeout=30, poll=poll)
        taken.add(cand)
        renamed += 1
    return {"renamed": renamed, "imports": len(by_iat)}


# ------------------------------------------------------------------------------------------------ signature packs
def sigpack_manifest() -> dict[str, Any]:
    try:
        return json.loads((SIGPACKS / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"packs": []}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def packs_for(fmt: str, arch: str, bits: int) -> list[dict[str, Any]]:
    out = []
    for p in sigpack_manifest().get("packs", []):
        if p.get("format") == fmt and p.get("arch") == arch and int(p.get("bits", 0)) == bits:
            f = SIGPACKS / p["file"]
            if f.is_file() and _sha(f) == p.get("sha256"):
                out.append({**p, "path": f})
    return out


def pass_sigpacks(sess: Any, poll) -> dict[str, Any]:
    text, _ = sess._exec("iIj", timeout=30, poll=poll)
    try:
        info = json.loads(text or "{}")
    except ValueError:
        info = {}
    fmt = {"pe": "pe", "pe64": "pe", "elf": "elf", "elf64": "elf"}.get(str(info.get("bintype") or info.get("class") or "").lower(),
                                                                     str(info.get("bintype") or "").lower())
    packs = packs_for(fmt, str(info.get("arch") or ""), int(info.get("bits") or 0))
    applied, skipped = [], []
    for p in packs:
        if not markers_present(sess.path, p.get("markers")):
            skipped.append({"pack": p["name"], "reason": "runtime markers not found in the file"})
            continue
        path = str(p["path"]).replace("\\", "/")
        if '"' in path or any(ord(c) < 32 for c in path):
            continue
        before = sum(1 for f in _functions(sess, poll) if str(f.get("name", "")).startswith("flirt."))
        sess._exec(f'Fs "{path}"', timeout=300, poll=poll)
        after = sum(1 for f in _functions(sess, poll) if str(f.get("name", "")).startswith("flirt."))
        applied.append({"pack": p["name"], "sha256": p["sha256"], "flirt_named_delta": after - before})
    return {"format": fmt, "arch": info.get("arch"), "bits": info.get("bits"), "applied": applied, "skipped": skipped}


def markers_present(path: Path, markers: dict[str, Any] | None) -> bool:
    """A pack applies only to files built with its runtime: ``head`` byte strings must occur in the first 1 KiB (e.g. the
    MS linker's ``Rich`` header), ``any`` strings anywhere in the file (e.g. Rust std's ``RUST_BACKTRACE``)."""
    if not markers:
        return True
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024 * 1024)
    except OSError:
        return False
    head = [m.encode("latin-1") for m in markers.get("head") or []]
    anyw = [m.encode("latin-1") for m in markers.get("any") or []]
    if head and not any(m in data[:1024] for m in head):
        return False
    return not anyw or any(m in data for m in anyw)


PASSES: dict[str, Callable[[Any, Any], dict[str, Any]]] = {"pdata": pass_pdata, "relocptrs": pass_relocptrs, "thunks": pass_thunks,
                                                          "sigpacks": pass_sigpacks}


def run_passes(sess: Any, names: list[str] | tuple[str, ...], poll) -> dict[str, Any]:
    """Run the named passes in a fixed order; a failing pass is reported, never fatal for the analysis."""
    report: dict[str, Any] = {}
    for n in ("sigpacks", "pdata", "relocptrs", "thunks"):
        if n not in names:
            continue
        try:
            report[n] = PASSES[n](sess, poll)
        except Exception as e:  # noqa: BLE001 - a pass failure must not lose the base analysis
            if getattr(sess, "_pipe", True) is None:   # timeout/crash/cancel dropped the process: let the caller handle it
                raise
            report[n] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    if "pdata" in names or "relocptrs" in names:
        # ``af`` at an address rizin had flagged as data names the function ``data.<addr>``: that is an auto name, so
        # say so (``fcn.<addr>``) instead of presenting it as a symbol.
        n = 0
        for f in _functions(sess, poll):
            m = re.fullmatch(r"data\.([0-9a-f]+)", str(f.get("name") or ""))
            if m and isinstance(f.get("offset"), int):
                sess._exec(f"afn fcn.{m.group(1)} @ {_addr(f['offset'])}", timeout=30, poll=poll)
                n += 1
        report["auto_names_fixed"] = n
    return report
