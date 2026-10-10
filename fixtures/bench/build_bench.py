#!/usr/bin/env python3
"""Build the R0 benchmark corpus (fixtures/bench/<row>/) and extract ground truth from the symbol-bearing build.

For every row this writes:
  <row>/bin/<binary>       the committed analysis input (stripped / optimized / packed / renamed)
  <row>/truth/truth.json   ground truth from the unstripped build or PDB (functions name/start/size, imports, strings, .NET types/methods)
  <row>/build.json         exact commands, flags, tool versions and hashes (the recorded build)
and fixtures/bench/manifest.json (every row, sha256, sizes, rows not built and why).

usage: python fixtures/bench/build_bench.py [--rows a,b] [--verify]
  --verify  rebuild into a temp dir and fail unless every committed binary is reproduced byte for byte.

Toolchains are discovered, never assumed: a row whose compiler is missing is recorded as "not built: <reason>".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parents[1]
sys.path.insert(0, str(BENCH / "tools"))
sys.path.insert(0, str(REPO / "controller"))
from pdb_truth import read_pdb  # noqa: E402

TRUTH_SCHEMA = "rebuild-studio.bench-truth/1"
MAX_BINARY = 5 * 1024 * 1024
MAX_TOTAL = 25 * 1024 * 1024
UPX_VERSION = "5.2.1"
UPX_EXE_SHA256 = "d20ebe0b7b22b6be968c8c34be61f94ddea12cb11462e2cec27f548ef9574df8"

C_STRINGS = ["benchc 1.0 - Rebuild Studio benchmark fixture", "usage: benchc <wc|crc|rle|b64|hist|sort|rev> <file>",
             "benchc: cannot open '%s'", "benchc: input larger than %u bytes", "lines=%lu words=%lu bytes=%lu longest=%lu",
             "crc32=%08lx size=%lu", "rle: %lu -> %lu bytes, ratio %.3f", "benchc: not a number: '%s'",
             "count=%d min=%ld max=%ld sum=%ld mean=%.2f",
             "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"]
CPP_STRINGS = ["benchcpp 1.0 - Rebuild Studio benchmark fixture", "circle radius must be positive", "rectangle sides must be positive",
               "triangle inequality violated", "benchcpp: invalid shape: %s", "shapes=%zu mean_area=%.3f max_area=%.3f",
               "%s area=%.3f perimeter=%.3f", "  (square perimeter %.1f)"]
GO_STRINGS = ["benchgo 1.0 - Rebuild Studio benchmark fixture", "benchgo: spec must look like name=qty@price",
              "benchgo: bad quantity in %q: %w", "benchgo: bad price in %q: %w", "total value: %.2f", "added %d distinct items"]
RS_STRINGS = ["benchrs 1.0 - Rebuild Studio benchmark fixture", "benchrs: cannot parse '", "matrix n=", "trace(m^3)=", "primes<="]
NET_STRINGS = ["benchnet 1.0 - Rebuild Studio benchmark fixture", "expected task:priority[:dep,dep]", "unknown priority '",
               "dependency cycle at '", "benchnet: bad spec '"]

ROWS: dict[str, dict] = {
    "c_msvc_x64_o2": {"kind": "native_pe_x64", "lang": "C", "builder": "msvc", "arch": "x64", "src": "c_msvc_x64_o2/src/benchc.c",
                      "binary": "benchc.exe", "strings": C_STRINGS, "own": {"module": "benchc.obj"},
                      "what": "C CLI, MSVC /O2, x64, PDB not shipped (stripped)"},
    "c_msvc_x86_o2": {"kind": "native_pe_x86", "lang": "C", "builder": "msvc", "arch": "x86", "src": "c_msvc_x64_o2/src/benchc.c",
                      "binary": "benchc32.exe", "strings": C_STRINGS, "own": {"module": "benchc.obj"},
                      "what": "same C source, MSVC /O2, 32-bit PE (x86), PDB not shipped"},
    "cpp_msvc_x64_o2": {"kind": "native_pe_x64", "lang": "C++", "builder": "msvc", "arch": "x64", "src": "cpp_msvc_x64_o2/src/benchcpp.cpp",
                        "binary": "benchcpp.exe", "strings": CPP_STRINGS, "own": {"module": "benchcpp.obj", "name_prefix": ["bench::", "main"]},
                        "what": "C++17 classes, vtables, RTTI, exceptions, templates; MSVC /O2 /EHsc /GR, x64"},
    "go_pe_x64": {"kind": "native_pe_x64", "lang": "Go", "builder": "go", "goos": "windows", "src": "go_pe_x64/src",
                  "binary": "benchgo.exe", "strings": GO_STRINGS, "own": {"name_prefix": ["main."]},
                  "what": "Go windows/amd64, -ldflags '-s -w' (symbol table and DWARF stripped)"},
    "go_elf_x64": {"kind": "native_elf_x64", "lang": "Go", "builder": "go", "goos": "linux", "src": "go_pe_x64/src",
                   "binary": "benchgo", "strings": GO_STRINGS, "own": {"name_prefix": ["main."]},
                   "what": "same Go source, linux/amd64 static ELF, stripped (-s -w); the stripped-ELF row"},
    "rust_msvc_x64": {"kind": "native_pe_x64", "lang": "Rust", "builder": "rust", "src": "rust_msvc_x64/src",
                      "binary": "benchrs.exe", "strings": RS_STRINGS, "own": {"name_prefix": ["benchrs::"]},
                      "what": "Rust release (opt-level 3), x86_64-pc-windows-msvc, PDB not shipped"},
    "upx_c_msvc_x64": {"kind": "native_pe_x64", "lang": "C", "builder": "upx", "base_row": "c_msvc_x64_o2",
                       "binary": "benchc_upx.exe", "strings": C_STRINGS, "own": {"module": "benchc.obj"},
                       "what": f"c_msvc_x64_o2 packed with UPX {UPX_VERSION} --best (truth = the unpacked build)"},
    "dotnet_plain": {"kind": "dotnet", "lang": "C#", "builder": "dotnet", "src": "dotnet_plain/src", "binary": "benchnet.dll",
                     "strings": NET_STRINGS, "what": ".NET 8 console (Release, Deterministic), names intact"},
    "dotnet_renamed": {"kind": "dotnet", "lang": "C#", "builder": "rename", "base_row": "dotnet_plain", "binary": "benchnet.dll",
                       "strings": NET_STRINGS,
                       "what": "dotnet_plain with ConfuserEx-style identifier renaming (fixtures/bench/tools/dotnet_rename.py)"},
}

NOT_BUILT = {
    "c_mingw_x64_o2s": "not run: mingw-w64 (x86_64-w64-mingw32-gcc) is not installed on this host",
    "c_mingw_x86_o2s": "not run: mingw-w64 (i686-w64-mingw32-gcc) is not installed on this host",
    "c_gcc_elf_stripped": "not run: no gcc/clang/zig for an ELF C build on this host (the stripped-ELF row is go_elf_x64)",
    "unity_il2cpp": "not run: needs the Unity editor with the IL2CPP module to build a project we own; not installed",
    "gamemaker": "not run: no free GameMaker runner/CLI build is available on this host",
    "dotnet_confuserex": "not run as ConfuserEx itself (.NET Framework GUI/CLI, unmaintained); renaming is reproduced by dotnet_rename.py (row dotnet_renamed)",
}


# ------------------------------------------------------------------------------------------------- helpers
def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> str:
    if env is not None and not os.path.isabs(cmd[0]):   # Windows resolves argv[0] on the parent's PATH, not env's
        exe = shutil.which(cmd[0], path=env.get("PATH") or env.get("Path"))
        cmd = [exe or cmd[0], *cmd[1:]]
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(cmd)}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r.stdout


def vswhere_install() -> Path | None:
    vw = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vw.is_file():
        return None
    out = subprocess.run([str(vw), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                          "-property", "installationPath"], capture_output=True, text=True).stdout.strip()
    return Path(out) if out else None


_ENV_CACHE: dict[str, dict] = {}


def msvc_env(arch: str) -> dict:
    if arch in _ENV_CACHE:
        return _ENV_CACHE[arch]
    inst = vswhere_install()
    if inst is None:
        raise RuntimeError("not run: MSVC (vswhere found no VC tools)")
    bat = inst / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    target = {"x64": "x64", "x86": "x64_x86"}[arch]
    r = subprocess.run(f'cmd /d /s /c ""{bat}" {target} >nul && set"', capture_output=True, text=True, shell=False)
    env = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    if "VCToolsVersion" not in env:
        raise RuntimeError(f"vcvarsall failed: {r.stderr[-500:]}")
    _ENV_CACHE[arch] = env
    return env


def msvc_version(env: dict) -> str:
    cl = shutil.which("cl", path=env.get("PATH") or env.get("Path")) or "cl"
    r = subprocess.run([cl], env=env, capture_output=True, text=True)
    m = re.search(r"Version ([\d.]+) for (\S+)", r.stderr)
    return f"MSVC cl {m.group(1)} ({m.group(2)}), VC tools {env.get('VCToolsVersion', '?').strip()}, Windows SDK {env.get('WindowsSDKVersion', '?').strip(chr(92))}" if m else "MSVC ?"


# ------------------------------------------------------------------------------------------------- PE/ELF facts
def pe_facts(path: Path) -> dict:
    import pefile
    pe = pefile.PE(str(path))
    base = pe.OPTIONAL_HEADER.ImageBase
    sections = [{"name": s.Name.rstrip(b"\0").decode("latin-1"), "start": base + s.VirtualAddress,
                 "end": base + s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData),
                 "exec": bool(s.Characteristics & 0x20000000)} for s in pe.sections]
    imports = []
    for d in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        lib = d.dll.decode("latin-1").lower()
        for imp in d.imports:
            if imp.name:
                imports.append({"lib": lib, "name": imp.name.decode("latin-1")})
    machine = pe.FILE_HEADER.Machine
    pe.close()
    return {"image_base": base, "sections": sections, "imports": imports, "machine": hex(machine)}


def elf_facts(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("not ELF64")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    hdrs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    strtab_off = hdrs[shstrndx][4]

    def nm(o: int) -> str:
        return data[strtab_off + o:data.index(b"\0", strtab_off + o)].decode("latin-1")
    sections = [{"name": nm(h[0]), "start": h[3], "end": h[3] + h[5], "exec": bool(h[2] & 0x4)} for h in hdrs if h[3]]
    return {"image_base": 0, "sections": sections, "imports": [], "machine": "x86_64"}


def section_bytes(path: Path, name: str) -> bytes:
    """Raw bytes of a named section (used to prove the stripped and unstripped builds share the same code)."""
    data = path.read_bytes()
    if data[:4] == b"\x7fELF":
        shoff, = struct.unpack_from("<Q", data, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
        hdrs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
        so = hdrs[shstrndx][4]
        for h in hdrs:
            if data[so + h[0]:data.index(b"\0", so + h[0])].decode() == name:
                return data[h[4]:h[4] + h[5]]
    else:
        import pefile
        pe = pefile.PE(data=data, fast_load=True)
        for s in pe.sections:
            if s.Name.rstrip(b"\0").decode("latin-1") == name:
                return s.get_data()
    raise KeyError(name)


def check_strings(binary: Path, strings: list[str], *, utf16: bool = False) -> None:
    data = binary.read_bytes()
    missing = [s for s in strings if s.encode("utf-16-le" if utf16 else "utf-8") not in data]
    if missing:
        raise RuntimeError(f"notable strings missing from {binary.name}: {missing}")


def is_own(name: str, module: str | None, rule: dict) -> bool:
    if "module" in rule and module != rule["module"]:
        return False
    if "name_prefix" in rule:
        return any(name == p or name.startswith(p) for p in rule["name_prefix"])
    return True


def truth_from_pdb(pdb: Path, exe: Path, own_rule: dict) -> tuple[list[dict], dict]:
    facts = pe_facts(exe)
    info = read_pdb(pdb)
    import pefile
    pe = pefile.PE(str(exe), fast_load=True)
    seg_va = {i + 1: facts["image_base"] + s.VirtualAddress for i, s in enumerate(pe.sections)}
    pe.close()
    exec_ranges = [(s["start"], s["end"]) for s in facts["sections"] if s["exec"]]

    def in_exec(va: int) -> bool:
        return any(a <= va < b for a, b in exec_ranges)
    by_start: dict[int, dict] = {}
    for p in info["procs"]:
        if p.segment not in seg_va:
            continue
        va = seg_va[p.segment] + p.offset
        if not in_exec(va):
            continue
        e = by_start.setdefault(va, {"start": va, "size": p.size, "names": [], "module": p.module, "source": "pdb.proc"})
        if p.name not in e["names"]:
            e["names"].append(p.name)
        e["size"] = max(e["size"] or 0, p.size)
    for pub in info["publics"]:
        if not pub.is_function or pub.segment not in seg_va:
            continue
        va = seg_va[pub.segment] + pub.offset
        if not in_exec(va):
            continue
        e = by_start.setdefault(va, {"start": va, "size": None, "names": [], "module": None, "source": "pdb.public"})
        if pub.name not in e["names"]:
            e["names"].append(pub.name)
    funcs = []
    for va in sorted(by_start):
        e = by_start[va]
        funcs.append({"name": e["names"][0], "aliases": e["names"][1:], "start": hex(va), "size": e["size"],
                      "module": e["module"], "source": e["source"],
                      "own": any(is_own(n, e["module"], own_rule) for n in e["names"])})
    return funcs, facts


def truth_from_go(sym_binary: Path, facts: dict, own_rule: dict) -> list[dict]:
    out = run(["go", "tool", "nm", "-size", "-sort", "address", str(sym_binary)])
    exec_ranges = [(s["start"], s["end"]) for s in facts["sections"] if s["exec"]]
    by_start: dict[int, dict] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4 or parts[2] not in ("T", "t"):
            continue
        va, size, name = int(parts[0], 16), int(parts[1]), parts[3]
        if size == 0 or not any(a <= va < b for a, b in exec_ranges):
            continue
        e = by_start.setdefault(va, {"names": [], "size": size})
        e["names"].append(name)
        e["size"] = max(e["size"], size)
    return [{"name": e["names"][0], "aliases": e["names"][1:], "start": hex(va), "size": e["size"], "module": None,
             "source": "go tool nm", "own": any(is_own(n, None, own_rule) for n in e["names"])}
            for va, e in sorted(by_start.items())]


def truth_dotnet(dll: Path) -> dict:
    from rebuild_controller.backends.ilspy import _Tables, read_clr_metadata
    import pefile
    md_info = read_clr_metadata(dll)
    data = dll.read_bytes()
    pe = pefile.PE(data=data, fast_load=True)
    dd = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14]
    md_rva, md_size = struct.unpack_from("<II", pe.get_data(dd.VirtualAddress, 72), 8)
    md = pe.get_data(md_rva, md_size)
    pe.close()
    t = _Tables(md)
    types = [{"name": x["name"], "kind": x["kind"], "nested": x["nested"]} for x in md_info["types"] if x["name"] != "<Module>"]
    n_types, n_methods = t.rows.get(0x02, 0), t.rows.get(0x06, 0)
    methods = []
    for i in range(1, n_types + 1):
        row = t.row(0x02, i)
        first = row[5]
        last = t.row(0x02, i + 1)[5] if i < n_types else n_methods + 1
        tname = md_info["types"][i - 1]["name"]
        if tname == "<Module>":
            continue
        for m in range(first, last):
            mr = t.row(0x06, m)
            name, flags = t.string(mr[3]), mr[2]
            methods.append({"type": tname, "name": name, "special_name": bool(flags & 0x800),
                            "compiler_generated": "<" in name or "<" in tname})
    return {"types": types, "methods": methods, "assembly": md_info["assembly"]}


# ------------------------------------------------------------------------------------------------- builders
def build_msvc(name: str, spec: dict, work: Path) -> dict:
    env = msvc_env(spec["arch"])
    src = BENCH / spec["src"]
    stem = src.stem
    obj, exe, pdb = work / f"{stem}.obj", work / spec["binary"], work / f"{Path(spec['binary']).stem}.pdb"
    cflags = ["/nologo", "/O2", "/MD", "/W3", "/GS", "/Zi", "/Brepro", "/DNDEBUG", "/utf-8"]
    if src.suffix == ".cpp":
        cflags += ["/EHsc", "/GR", "/std:c++17"]
    lflags = ["/nologo", "/DEBUG:FULL", f"/PDB:{pdb.name}", f"/PDBALTPATH:{pdb.name}", "/OPT:REF", "/OPT:ICF", "/INCREMENTAL:NO",
              "/Brepro", "/SUBSYSTEM:CONSOLE"]
    shutil.copy2(src, work / src.name)
    run(["cl", *cflags, f"/Fd:{stem}.compile.pdb", "/c", src.name, f"/Fo:{obj.name}"], cwd=work, env=env)
    run(["link", *lflags, f"/OUT:{exe.name}", obj.name], cwd=work, env=env)
    funcs, facts = truth_from_pdb(pdb, exe, spec["own"])
    return {"binary": exe, "functions": funcs, "facts": facts, "truth_source": f"{pdb.name} (MSF 7.00, read by tools/pdb_truth.py)",
            "toolchain": msvc_version(env),
            "commands": [f"cl {' '.join(cflags)} /c {src.name}", f"link {' '.join(lflags)} /OUT:{exe.name} {obj.name}"],
            "stripped_how": "the PDB is not shipped; the exe only names it (PDBALTPATH) and carries no symbol table"}


def build_go(name: str, spec: dict, work: Path) -> dict:
    src = BENCH / spec["src"]
    env = {**os.environ, "GOOS": spec["goos"], "GOARCH": "amd64", "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local",
           "GOCACHE": str(work / "gocache")}
    sym = work / ("sym_" + spec["binary"])
    out = work / spec["binary"]
    base = ["go", "build", "-trimpath", "-buildvcs=false"]
    run([*base, "-ldflags=-buildid=", "-o", str(sym), "."], cwd=src, env=env)
    run([*base, "-ldflags=-s -w -buildid=", "-o", str(out), "."], cwd=src, env=env)
    if section_bytes(sym, ".text") != section_bytes(out, ".text"):
        raise RuntimeError("stripped and unstripped Go builds differ in .text; truth would not apply")
    facts = pe_facts(out) if spec["goos"] == "windows" else elf_facts(out)
    funcs = truth_from_go(sym, facts, spec["own"])
    ver = run(["go", "version"]).strip()
    return {"binary": out, "functions": funcs, "facts": facts,
            "truth_source": "go tool nm -size -sort address on the unstripped build (identical .text, verified)",
            "toolchain": ver, "commands": [f"GOOS={spec['goos']} GOARCH=amd64 CGO_ENABLED=0 {' '.join(base)} -ldflags='-s -w -buildid=' -o {out.name} .",
                                           f"(truth build) {' '.join(base)} -ldflags=-buildid= -o sym_{out.name} ."],
            "stripped_how": "-ldflags '-s -w' removes the symbol table and DWARF (pclntab remains, as in every Go binary)"}


def build_rust(name: str, spec: dict, work: Path) -> dict:
    src = BENCH / spec["src"]
    proj = work / "proj"
    shutil.copytree(src, proj)
    tdir = work / "target"
    flags = f"--remap-path-prefix={proj}=benchrs -C link-arg=/Brepro -C link-arg=/PDBALTPATH:benchrs.pdb"
    env = {**os.environ, "CARGO_TARGET_DIR": str(tdir), "RUSTFLAGS": flags, "CARGO_INCREMENTAL": "0"}
    env.pop("RUSTC_WRAPPER", None)
    run(["cargo", "build", "--release", "--locked" if (proj / "Cargo.lock").exists() else "--offline", "--target", "x86_64-pc-windows-msvc", "-q"],
        cwd=proj, env=env)
    rel = tdir / "x86_64-pc-windows-msvc" / "release"
    exe = work / spec["binary"]
    shutil.copy2(rel / "benchrs.exe", exe)
    funcs, facts = truth_from_pdb(rel / "benchrs.pdb", exe, spec["own"])
    return {"binary": exe, "functions": funcs, "facts": facts, "truth_source": "benchrs.pdb (MSF 7.00, read by tools/pdb_truth.py)",
            "toolchain": run(["rustc", "-V"]).strip() + "; " + run(["cargo", "-V"]).strip(),
            "commands": [f"RUSTFLAGS='{flags.replace(str(proj), '<src>')}' cargo build --release --target x86_64-pc-windows-msvc",
                         "Cargo.toml [profile.release]: opt-level=3 debug=2 strip=none codegen-units=1 lto=false panic=unwind"],
            "stripped_how": "the PDB is not shipped; MSVC-target executables carry no symbol table"}


def find_upx() -> Path:
    cands = [Path(os.environ["REBUILD_STUDIO_TOOLS"]) / "upx" / "upx.exe"] if os.environ.get("REBUILD_STUDIO_TOOLS") else []
    cands.append(Path(os.environ.get("LOCALAPPDATA", "")) / "RebuildStudio" / "tools" / "upx" / "upx.exe")
    w = shutil.which("upx")
    if w:
        cands.append(Path(w))
    for c in cands:
        if c.is_file():
            if sha256(c) != UPX_EXE_SHA256:
                raise RuntimeError(f"{c}: sha256 differs from the pinned upx {UPX_VERSION} (docs/dependency-lock.json fixture_build_tools.upx)")
            return c
    raise RuntimeError(f"not run: upx {UPX_VERSION} not found (install the pinned release into <tools>/upx/)")


def build_upx(name: str, spec: dict, work: Path) -> dict:
    upx = find_upx()
    base = build_msvc(spec["base_row"], ROWS[spec["base_row"]], work / "base")
    out = work / spec["binary"]
    run([str(upx), "--best", "--no-color", "-q", "-o", str(out), str(base["binary"])])
    return {**base, "binary": out, "packed": True, "packer": f"UPX {UPX_VERSION}",
            "toolchain": base["toolchain"] + f"; upx {UPX_VERSION} (sha256 {UPX_EXE_SHA256[:16]}...)",
            "commands": base["commands"] + [f"upx --best --no-color -q -o {out.name} benchc.exe"],
            "stripped_how": "packed: original sections compressed into UPX0/UPX1; truth is the unpacked build's PDB"}


def build_dotnet(name: str, spec: dict, work: Path) -> dict:
    src = BENCH / spec["src"]
    proj = work / "proj"
    shutil.copytree(src, proj)
    env = {**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1"}
    run(["dotnet", "build", str(proj / "benchnet.csproj"), "-c", "Release", "-o", str(work / "out"), "--nologo", "-v", "q"], env=env)
    dll = work / spec["binary"]
    shutil.copy2(work / "out" / "benchnet.dll", dll)
    return {"binary": dll, "dotnet": truth_dotnet(dll), "truth_source": "ECMA-335 metadata of the unobfuscated build",
            "toolchain": "dotnet SDK " + run(["dotnet", "--version"], env=env).strip(),
            "commands": ["dotnet build benchnet.csproj -c Release (Deterministic, Optimize, DebugType=none)"],
            "stripped_how": "no PDB (DebugType=none)"}


def build_rename(name: str, spec: dict, work: Path) -> dict:
    from dotnet_rename import rename
    base = build_dotnet(spec["base_row"], ROWS[spec["base_row"]], work / "base")
    out = work / spec["binary"]
    rep = rename(base["binary"], out)
    return {**base, "binary": out, "rename_report": rep,
            "commands": base["commands"] + ["python fixtures/bench/tools/dotnet_rename.py benchnet.dll <out>/benchnet.dll"],
            "stripped_how": f"identifiers renamed in the #Strings heap ({rep['renamed_count']} renamed, {rep['skipped_count']} kept)"}


BUILDERS = {"msvc": build_msvc, "go": build_go, "rust": build_rust, "upx": build_upx, "dotnet": build_dotnet, "rename": build_rename}


def build_row(name: str, work: Path) -> dict:
    spec = ROWS[name]
    work.mkdir(parents=True, exist_ok=True)
    for sub in ("base",):
        (work / sub).mkdir(exist_ok=True)
    return BUILDERS[spec["builder"]](name, spec, work)


def write_row(name: str, res: dict) -> dict:
    spec = ROWS[name]
    row_dir = BENCH / name
    (row_dir / "bin").mkdir(parents=True, exist_ok=True)
    (row_dir / "truth").mkdir(parents=True, exist_ok=True)
    dest = row_dir / "bin" / spec["binary"]
    shutil.copy2(res["binary"], dest)
    size = dest.stat().st_size
    if size > MAX_BINARY:
        raise RuntimeError(f"{name}: binary {size} bytes exceeds {MAX_BINARY}")
    if spec["kind"] == "dotnet":
        check_strings(dest if spec["builder"] == "dotnet" else res["binary"], spec["strings"], utf16=True)
    elif not res.get("packed"):
        check_strings(dest, spec["strings"])
    truth = {"schema": TRUTH_SCHEMA, "row": name, "kind": spec["kind"], "language": spec["lang"], "what": spec["what"],
             "binary": {"path": f"bin/{spec['binary']}", "sha256": sha256(dest), "size": size},
             "truth_source": res["truth_source"], "packed": bool(res.get("packed")), "packer": res.get("packer"),
             "strings": spec["strings"]}
    if spec["kind"] == "dotnet":
        truth["dotnet"] = res["dotnet"]
        if "rename_report" in res:
            truth["rename_report"] = res["rename_report"]
    else:
        f = res["facts"]
        truth.update({"image_base": hex(f["image_base"]), "machine": f["machine"],
                      "code_ranges": [[hex(s["start"]), hex(s["end"])] for s in f["sections"] if s["exec"]],
                      "imports": f["imports"], "functions": res["functions"],
                      "counts": {"functions": len(res["functions"]), "own_functions": sum(1 for x in res["functions"] if x["own"]),
                                 "imports": len(f["imports"])}})
    (row_dir / "truth" / "truth.json").write_text(json.dumps(truth, indent=1) + "\n", encoding="utf-8", newline="\n")
    build = {"row": name, "what": spec["what"], "toolchain": res["toolchain"], "commands": res["commands"],
             "stripped_how": res["stripped_how"], "binary_sha256": truth["binary"]["sha256"], "binary_size": size,
             "source": spec.get("src") or ROWS[spec["base_row"]].get("src"), "reproduce": f"python fixtures/bench/build_bench.py --rows {name}"}
    (row_dir / "build.json").write_text(json.dumps(build, indent=1) + "\n", encoding="utf-8", newline="\n")
    return build


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the R0 benchmark corpus")
    ap.add_argument("--rows", default=",".join(ROWS))
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    rows = [r for r in a.rows.split(",") if r]
    built, failed = {}, {}
    with tempfile.TemporaryDirectory(prefix="rs-bench-") as tmp:
        for name in rows:
            try:
                res = build_row(name, Path(tmp) / name)
            except Exception as e:  # report and continue: a missing compiler is "not built", never a pass
                failed[name] = str(e).splitlines()[0][:400]
                print(f"{name}: NOT BUILT: {failed[name]}")
                continue
            if a.verify:
                committed = BENCH / name / "bin" / ROWS[name]["binary"]
                same = committed.is_file() and sha256(committed) == sha256(res["binary"])
                print(f"{name}: {'reproduced' if same else 'DIFFERS'} {sha256(res['binary'])[:16]}")
                if not same:
                    failed[name] = "rebuild differs from the committed binary"
                continue
            built[name] = write_row(name, res)
            print(f"{name}: built {built[name]['binary_size']} bytes sha256 {built[name]['binary_sha256'][:16]}")
    if not a.verify:
        man_p = BENCH / "manifest.json"
        man = json.loads(man_p.read_text(encoding="utf-8")) if man_p.is_file() else {}
        rows_doc = man.get("rows", {})
        for name, b in built.items():
            rows_doc[name] = {"kind": ROWS[name]["kind"], "binary": f"{name}/bin/{ROWS[name]['binary']}", "sha256": b["binary_sha256"],
                              "size": b["binary_size"], "truth": f"{name}/truth/truth.json", "toolchain": b["toolchain"]}
        total = sum(r["size"] for r in rows_doc.values())
        if total > MAX_TOTAL:
            raise SystemExit(f"corpus total {total} exceeds {MAX_TOTAL}")
        doc = {"schema": "rebuild-studio.bench-manifest/1", "rows": dict(sorted(rows_doc.items())), "total_binary_bytes": total,
               "not_built": {**NOT_BUILT, **{k: v for k, v in failed.items() if k not in rows_doc}}}
        man_p.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
