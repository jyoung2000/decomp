#!/usr/bin/env python3
"""Build the FLIRT signature packs shipped in controller/rebuild_controller/data/sigpacks/ (R1).

The packs name compiler-runtime functions in stripped programs. They are generated from the runtime LIBRARIES themselves
(COFF objects inside .lib archives and Rust .rlib archives), never from the benchmark corpus:

  msvc-<toolset>-crt   x64 + x86: msvcrt.lib (static part of /MD), libcmt.lib (/MT startup), libvcruntime.lib,
                       msvcprt.lib (static part of the /MD C++ library), libcpmt.lib (/MT C++ library)
  rust-<version>-std   x86_64-pc-windows-msvc: every runtime .rlib of the toolchain (std, core, alloc, panic_unwind, ...)

For every object, rizin analyses it (its own COFF symbol table gives the names) and writes a .pat with ``Fc``; names are
demangled (MSVC / Rust v0 + legacy / Itanium) to qualified names; functions shorter than --min-len bytes and patterns that
map to different names (ambiguous) are dropped; the merged .pat is pinned by sha256 in data/sigpacks/manifest.json together
with the source library hashes, toolset version and rizin version.

usage: python scripts/build_sigpacks.py [--packs msvc,rust] [--jobs 8] [--min-len 12]
Needs: rizin (REBUILD_STUDIO_TOOLS), MSVC Build Tools (vswhere) for msvc, rustup's stable msvc toolchain for rust.
Signatures only match code built by the SAME toolset/version; other versions need their own pack (rerun on that host).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "controller"))
OUT = REPO / "controller" / "rebuild_controller" / "data" / "sigpacks"

MSVC_LIBS = ["msvcrt.lib", "libcmt.lib", "libvcruntime.lib", "msvcprt.lib", "libcpmt.lib"]
# A pack is applied only to files that carry its runtime's markers (rizin_passes.markers_present): the MS linker's "Rich"
# header for the MSVC runtime, Rust std's own strings for the Rust pack.
MARKERS = {"msvc": {"head": ["Rich"]}, "rust": {"any": ["RUST_BACKTRACE", "/rustc/"]}}
RUST_SKIP = re.compile(r"^lib(proc_macro|test|getopts|unicode_width|rustc_std_workspace_\w+|profiler_builtins|panic_abort|sysroot)-")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ar_members(data: bytes):
    """Members of a System V / COFF ``!<arch>`` archive (.lib, .rlib): (name, bytes)."""
    if data[:8] != b"!<arch>\n":
        raise ValueError("not an ar archive")
    off, longnames = 8, b""
    while off + 60 <= len(data):
        h = data[off:off + 60]
        name = h[:16].decode("latin-1").rstrip()
        size = int(h[48:58].decode().strip())
        body = data[off + 60: off + 60 + size]
        if name == "//":
            longnames = body
        elif name not in ("/", "", "/SYM64/"):
            if name.startswith("/") and name[1:].isdigit():
                i = int(name[1:])
                j = longnames.find(b"\0", i)
                j = longnames.find(b"/\n", i) if j < 0 else j
                name = longnames[i:j].decode("latin-1")
            yield name.rstrip("/"), body
        off += 60 + size + (size & 1)


def is_coff_object(b: bytes) -> bool:
    if len(b) < 20 or b[:4] == b"\0\0\xff\xff":       # short import-library member
        return False
    return int.from_bytes(b[:2], "little") in (0x8664, 0x14C)


def find_rizin() -> Path:
    from rebuild_controller.backends.rizin_worker import find_rizin as fr
    t = fr()
    if t is None:
        raise SystemExit("rizin not found (set REBUILD_STUDIO_TOOLS)")
    return t.exe


def rizin_version(rz: Path) -> str:
    return subprocess.run([str(rz), "-v"], capture_output=True, text=True).stdout.splitlines()[0]


def pat_for_object(rz: Path, obj: Path) -> list[str]:
    """rizin: analyse the object (its COFF symbols define the functions), write a .pat, and map rizin's sanitised
    function names back to the object's real (mangled) symbol names via ``isj`` so they can be demangled."""
    out = obj.with_suffix(".pat")
    r = subprocess.run([str(rz), "-q", "-e", "flirt.ignore.unknown=true", "-c", "aaa", "-c", f'Fc "{out.as_posix()}"',
                        "-c", "isj", str(obj)], capture_output=True, timeout=600)
    if not out.is_file():
        return []
    real: dict[str, str] = {}
    for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        if ln.startswith("["):
            try:
                for sym in json.loads(ln):
                    fl, rn = str(sym.get("flagname") or ""), sym.get("realname")
                    if rn and fl.startswith("sym.") and not sym.get("is_imported"):
                        real.setdefault(fl[4:], rn)
            except ValueError:
                pass
    lines = []
    for ln in out.read_text(encoding="latin-1").splitlines():
        f = ln.split()
        if len(f) >= 6 and f[4].startswith(":"):
            f[5] = real.get(f[5], f[5])
            if any(c.isspace() for c in f[5]):
                continue
            lines.append(" ".join(f[:6]))
    return lines


def demangle_all(rz: Path, names: set[str]) -> dict[str, str]:
    import rzpipe
    from rebuild_controller.backends.demangle import guess_lang, qualified_name
    out: dict[str, str] = {}
    env_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(rz.parent) + os.pathsep + env_path
    r = rzpipe.open("--")
    try:
        for n in sorted(names):
            raw = re.sub(r"^sym\.", "", n)
            lang = guess_lang(raw)
            if lang and '"' not in raw:
                d = (r.cmd(f'iD {lang} "{raw}"') or "").strip()
                out[n] = qualified_name(lang, d) if d and d != raw else raw
            else:
                # x86 C decorations: _name, _name@8 (stdcall), @name@8 (fastcall)
                m = re.fullmatch(r"[_@]?([A-Za-z_][A-Za-z0-9_]*)@\d+", raw)
                out[n] = m.group(1) if m else raw
    finally:
        r.quit()
        os.environ["PATH"] = env_path
    return out


def build_pack(name: str, fmt: str, arch: str, bits: int, archives: list[Path], rz: Path, jobs: int, min_len: int,
               provenance: dict) -> dict:
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="rs-sigpack-") as tmp:
        objs: list[Path] = []
        for ai, a in enumerate(archives):
            for mi, (mname, body) in enumerate(ar_members(a.read_bytes())):
                if not is_coff_object(body):
                    continue
                p = Path(tmp) / f"{ai:02d}_{mi:05d}.obj"
                p.write_bytes(body)
                objs.append(p)
        lines: list[str] = []
        with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
            for res in ex.map(lambda o: pat_for_object(rz, o), objs):
                lines += res
    parsed = []
    for ln in lines:
        f = ln.split()
        if len(f) < 6 or not f[4].startswith(":"):
            continue
        parsed.append((f[0], f[1], f[2], f[3], f[5]))
    names = demangle_all(rz, {p[4] for p in parsed})
    by_key: dict[tuple, set[str]] = {}
    for pat, cl, crc, ln_hex, nm in parsed:
        if int(ln_hex, 16) < min_len:
            continue
        by_key.setdefault((pat, cl, crc, ln_hex), set()).add(names.get(nm, nm))
    from rebuild_controller.backends.demangle import pat_safe
    kept = sorted((k, next(iter(v))) for k, v in by_key.items() if len(v) == 1)
    ambiguous = sum(1 for v in by_key.values() if len(v) > 1)
    rel = Path(fmt) / arch / str(bits) / f"{name}.pat"
    dest = OUT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k[0]} {k[1]} {k[2]} {k[3]} :0000 {tidy_pat_name(pat_safe(n))}\n" for k, n in kept) + "---\n"
    dest.write_text(body, encoding="latin-1", newline="\n")
    return {"name": name, "file": rel.as_posix(), "format": fmt, "arch": arch, "bits": bits, "sha256": sha256(dest),
            "signatures": len(kept), "dropped_ambiguous": ambiguous, "objects": len(objs), "min_len": min_len,
            "seconds": round(time.time() - t0, 1), **provenance,
            "sources": [{"file": a.name, "sha256": sha256(a), "size": a.stat().st_size} for a in archives]}


_PAT_ELAB = re.compile(r"(?<=[<,(])_(?:class|struct|enum|union)_")


def tidy_pat_name(n: str) -> str:
    """Same tidy as demangle.tidy, on a name already made whitespace-free by pat_safe (spaces became '_')."""
    n = _PAT_ELAB.sub("", n.replace("___ptr64", ""))
    n = n.replace(",_", ",")
    from rebuild_controller.backends.demangle import strip_inherent
    return strip_inherent(re.sub(r"@@[0-9]+$", "", n), "_as_")


def retidy(man: dict) -> None:
    """Rewrite the names of already-built packs with tidy_pat_name and refresh their sha256 (no rizin run needed)."""
    for p in man.get("packs", []):
        f = OUT / p["file"]
        out = []
        for ln in f.read_text(encoding="latin-1").splitlines():
            parts = ln.split(" ")
            if len(parts) >= 6:
                parts[5] = tidy_pat_name(parts[5])
            out.append(" ".join(parts))
        f.write_text("\n".join(out) + "\n", encoding="latin-1", newline="\n")
        p["sha256"] = sha256(f)


def msvc_root() -> Path:
    sys.path.insert(0, str(REPO / "fixtures" / "bench"))
    from build_bench import vswhere_install
    inst = vswhere_install()
    if inst is None:
        raise SystemExit("MSVC not found (vswhere)")
    vers = sorted((inst / "VC" / "Tools" / "MSVC").iterdir())
    return vers[-1]


def rust_lib_dir() -> tuple[Path, str]:
    sysroot = subprocess.run(["rustc", "--print", "sysroot"], capture_output=True, text=True).stdout.strip()
    ver = subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout.strip()
    return Path(sysroot) / "lib" / "rustlib" / "x86_64-pc-windows-msvc" / "lib", ver


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs", default="msvc,rust")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--min-len", type=int, default=12)
    ap.add_argument("--retidy", action="store_true", help="only re-tidy the names of the packs already built")
    a = ap.parse_args(argv)
    if a.retidy:
        mp = OUT / "manifest.json"
        man = json.loads(mp.read_text(encoding="utf-8"))
        retidy(man)
        for p in man["packs"]:
            p["markers"] = MARKERS[p["name"].split("-")[0]]
        mp.write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"re-tidied {len(man['packs'])} packs")
        return 0
    rz = find_rizin()
    rzv = rizin_version(rz)
    man_path = OUT / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8")) if man_path.is_file() else {"packs": []}
    packs = {p["name"] + "/" + p["arch"] + str(p["bits"]): p for p in man.get("packs", [])}
    want = set(a.packs.split(","))
    if "msvc" in want:
        root = msvc_root()
        tv = root.name
        for arch_dir, bits in (("x64", 64), ("x86", 32)):
            libs = [root / "lib" / arch_dir / n for n in MSVC_LIBS if (root / "lib" / arch_dir / n).is_file()]
            p = build_pack(f"msvc-{tv}-crt", "pe", "x86", bits, libs, rz, a.jobs, a.min_len,
                           {"toolset": f"MSVC {tv} ({arch_dir})", "rizin": rzv, "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            p["markers"] = MARKERS["msvc"]
            packs[p["name"] + "/" + p["arch"] + str(p["bits"])] = p
            print(json.dumps({k: p[k] for k in ("name", "bits", "signatures", "dropped_ambiguous", "objects", "seconds")}))
    if "rust" in want:
        d, ver = rust_lib_dir()
        libs = sorted(x for x in d.glob("*.rlib") if not RUST_SKIP.match(x.name))
        short = ver.split()[1]
        p = build_pack(f"rust-{short}-std", "pe", "x86", 64, libs, rz, a.jobs, a.min_len,
                       {"toolset": f"{ver} x86_64-pc-windows-msvc", "rizin": rzv, "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        p["markers"] = MARKERS["rust"]
        packs[p["name"] + "/" + p["arch"] + str(p["bits"])] = p
        print(json.dumps({k: p[k] for k in ("name", "bits", "signatures", "dropped_ambiguous", "objects", "seconds")}))
    man = {"schema": "rebuild-studio.sigpacks/1",
           "$comment": "FLIRT .pat packs applied by rizin_passes.pass_sigpacks (Fs) after aaa; generated by scripts/build_sigpacks.py "
                       "from the runtime libraries (never from the benchmark corpus). A pack whose sha256 differs is not applied.",
           "packs": sorted(packs.values(), key=lambda p: p["file"])}
    man_path.write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {man_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
