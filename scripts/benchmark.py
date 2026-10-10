#!/usr/bin/env python3
"""R0 benchmark: score Rebuild Studio's no-AI analysis against ground truth and write reports/benchmark.{json,md}.

Corpus: fixtures/bench/<row>/ (bin/ + truth/truth.json, built by fixtures/bench/build_bench.py) plus the original acceptance
fixtures (pecli, dotnetapp, javacli, godotgame, webapp), each listed with what can and cannot be scored.

The analysis is the product's own backends, not a parallel analyzer:
* native rows -> rebuild_controller.backends.rizin_worker.RizinBackend (rizin 0.9.1 + rz-ghidra), sessions via session_for_path;
* .NET rows   -> rebuild_controller.backends.ilspy.ILSpyBackend.decompile (ilspycmd).

Metrics (native): function-boundary recall/precision (exact start address; precision counts only functions found inside
executable sections), named-function recall, real-decompiler coverage of our own source functions, decompile failures,
imports recall, notable-strings recall, packed detected, per-stage seconds.  (.NET): type recall, method recall,
type decompile coverage, decompile failures, strings recall.

Native-language rebuild (R4): for .NET rows and javacli the product pipeline rebuilds the recovered C#/Java (target csharp/java,
AI off) in a temp data folder; the column reports whether it builds, the deterministic fixes applied and, where the fixture has a
scenario oracle, how many scenarios match. Native-code rows have no original-language source to rebuild (they are Rust ports).

usage: python scripts/benchmark.py [--rows a,b] [--config cfg.json] [--out-dir reports] [--no-legacy] [--only-native-rebuild]
Config keys (all optional; defaults in DEFAULT_CONFIG):
  {"name": "default", "analysis": {"command": "aaa", "analysis.timeout": 300, "passes": ["sigpacks", "pdata", "relocptrs", "thunks"]},
   "decompile": {"max_functions": 200}, "strings": true, "imports": true, "dotnet": {"enabled": true, "timeout": 600},
   "packer": {"check": true, "unpack": true}}
A row that cannot run (tool missing, binary missing) is reported as "not run: <reason>", never as 0 or as a pass.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "fixtures" / "bench"
sys.path.insert(0, str(REPO / "controller"))

TRUTH_SCHEMA = "rebuild-studio.bench-truth/1"
REPORT_SCHEMA = "rebuild-studio.benchmark/1"
DEFAULT_CONFIG: dict[str, Any] = {
    "name": "default",
    "analysis": {"command": "aaa", "analysis.timeout": 300, "passes": ["sigpacks", "pdata", "relocptrs", "thunks"]},
    "decompile": {"max_functions": 200},
    "strings": True,
    "imports": True,
    "dotnet": {"enabled": True, "timeout": 600},
    "packer": {"check": True, "unpack": True},
    "native_rebuild": {"enabled": True, "timeout": 900},
    # Optional second decompiler (full Ghidra headless, whole-program run). Off by default: needs Ghidra 12.1.4 + JDK 21.
    "ghidra": {"enabled": False, "max_functions": 2000, "per_function_timeout": 60, "skip_rows": ["go_pe_x64", "go_elf_x64"]},
}
# A deliberately weaker analyzer: shallow analysis (no call-target recursion) and no decompilation. Used by the guard test.
WEAK_CONFIG: dict[str, Any] = {"name": "weak", "analysis": {"command": "aa", "analysis.timeout": 300, "passes": []},
                               "decompile": {"max_functions": 0}}

LEGACY = {
    "pecli": {"kind": "native_pe_x64", "binary": "fixtures/pecli/original/pecli.exe",
              "why_partial": "no symbol map: shipped stripped (mingw -O1 -s) and mingw is not on this host to rebuild an unstripped twin; "
                             "only functions-found / imports / strings found are reported"},
    "dotnetapp": {"kind": "dotnet", "binary": "fixtures/dotnetapp/original/dotnetapp.dll",
                  "why_partial": "names are not obfuscated, so the assembly's own metadata is the type/method truth"},
    "javacli": {"kind": "jvm", "not_scored": "not scored by R0 metrics: JVM jar (CFR path); parity is measured by its scenario oracle",
                "binary": "fixtures/javacli/original/javacli.jar"},
    "godotgame": {"kind": "godot", "not_scored": "not scored by R0 metrics: Godot PCK (GDRE path); recovery is checked by expected/resources.json"},
    "webapp": {"kind": "web", "not_scored": "not scored by R0 metrics: web/asar (no native code); parity is measured by its Playwright oracle"},
}


# ================================================================================================ config
def merge_config(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_config(out[k], v)
        else:
            out[k] = v
    return out


# ================================================================================================ scoring (pure functions)
_RZ_PREFIXES = ("sym.imp.", "sym.", "dbg.", "method.", "imp.", "reloc.", "flirt.", "go.")
_RZ_AUTONAMES = re.compile(r"^(fcn|sub|loc|entry|section|case|switch|int|unk)[._0-9]", re.I)


def as_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def canonical(name: str | None) -> str:
    """Compare names modulo rizin's flag sanitising, leading underscores, case and punctuation."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def analyzer_name(name: str | None) -> str | None:
    """The meaningful part of an analyzer function name, or None for auto names (fcn.1400..., entry0, sub.*)."""
    n = name or ""
    changed = True
    while changed:
        changed = False
        for p in _RZ_PREFIXES:
            if n.startswith(p):
                n, changed = n[len(p):], True
    if not n or _RZ_AUTONAMES.match(n) or re.fullmatch(r"(fcn|entry)\d*", n):
        return None
    return n


def ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def in_ranges(addr: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= addr < b for a, b in ranges)


def score_functions(truth_funcs: list[dict], found: list[dict], code_ranges: list[tuple[int, int]]) -> dict:
    """Function-boundary recall/precision (exact start) and named-function recall, overall and for our own source functions.

    truth_funcs: [{"name", "aliases", "start", "size", "own"}]; found: [{"offset", "name", "size"}] (rizin aflj shape).
    """
    truth = {as_int(f["start"]): f for f in truth_funcs}
    found_by_start: dict[int, dict] = {}
    for f in found:
        off = as_int(f.get("offset", f.get("addr")))
        if off is not None:
            found_by_start.setdefault(off, f)
    in_code = [o for o in found_by_start if in_ranges(o, code_ranges)] if code_ranges else list(found_by_start)
    matched = [s for s in truth if s in found_by_start]
    own = [s for s, f in truth.items() if f.get("own")]
    own_matched = [s for s in own if s in found_by_start]

    def named_ok(s: int) -> bool:
        f = found_by_start.get(s)
        if f is None:
            return False
        got = analyzer_name(f.get("name"))
        if got is None:
            return False
        want = {canonical(n) for n in [truth[s]["name"], *truth[s].get("aliases", [])]}
        return canonical(got) in want
    named = [s for s in truth if named_ok(s)]
    # name precision: of the matched functions that carry a non-auto name, how many carry the right one (a wrong name is
    # worse than fcn.*: it misleads the reader)
    with_name = [s for s in matched if analyzer_name(found_by_start[s].get("name")) is not None]
    sized = [s for s in matched if truth[s].get("size")]
    size_exact = [s for s in sized if as_int(found_by_start[s].get("size")) == truth[s]["size"]]
    return {
        "truth_functions": len(truth), "found_functions": len(found_by_start), "found_in_code": len(in_code),
        "matched": len(matched), "recall": ratio(len(matched), len(truth)), "precision": ratio(len(matched), len(in_code)),
        "own_truth": len(own), "own_matched": len(own_matched), "own_recall": ratio(len(own_matched), len(own)),
        "named": len(named), "named_recall": ratio(len(named), len(truth)),
        "own_named_recall": ratio(sum(1 for s in own if s in named), len(own)),
        "named_precision": ratio(sum(1 for s in with_name if s in named), len(with_name)), "with_name": len(with_name),
        "size_exact": ratio(len(size_exact), len(sized)),
    }


def score_imports(truth_imports: list[dict], found: list[dict]) -> dict:
    want = {(i["name"]) for i in truth_imports}
    got = {str(i.get("name", "")) for i in found}
    # rizin may prefix with the library ("KERNEL32.dll_GetStdHandle"); accept a suffix match on "_<name>"
    hit = {w for w in want if w in got or any(g.endswith("_" + w) for g in got)}
    return {"truth": len(want), "found": len(got), "matched": len(hit), "recall": ratio(len(hit), len(want)),
            "missing_sample": sorted(want - hit)[:10]}


def score_strings(truth_strings: list[str], found: list[str]) -> dict:
    blob = "\n".join(found)
    hit = [s for s in truth_strings if s in blob]
    return {"truth": len(truth_strings), "found": len(found), "matched": len(hit), "recall": ratio(len(hit), len(truth_strings)),
            "missing": [s for s in truth_strings if s not in hit][:10]}


_CS_TYPE_RE = re.compile(r"\b(?:class|struct|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)")
_CS_DELEGATE_RE = re.compile(r"\bdelegate\s+[\w<>\[\],. ?]+?\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CS_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^<>()]*>)?\s*\(")


def score_dotnet(truth: dict, csharp: str) -> dict:
    """Type and method recall of the unobfuscated metadata names in decompiled C#."""
    types = [t for t in truth["types"] if "<" not in t["name"]]
    declared = set(_CS_TYPE_RE.findall(csharp)) | set(_CS_DELEGATE_RE.findall(csharp))
    t_hit = [t for t in types if t["name"].rsplit(".", 1)[-1] in declared]
    methods = [m for m in truth["methods"] if not m["special_name"] and not m["compiler_generated"]]
    idents = set(_CS_CALL_RE.findall(csharp))
    m_hit = [m for m in methods if m["name"] in idents]
    return {"truth_types": len(types), "types_matched": len(t_hit), "type_recall": ratio(len(t_hit), len(types)),
            "truth_methods": len(methods), "methods_matched": len(m_hit), "method_recall": ratio(len(m_hit), len(methods)),
            "missing_types": sorted(t["name"] for t in types if t not in t_hit)[:10],
            "missing_methods": sorted(f"{m['type']}::{m['name']}" for m in methods if m not in m_hit)[:10]}


def load_truth(path: Path) -> dict:
    t = json.loads(path.read_text(encoding="utf-8"))
    if t.get("schema") != TRUTH_SCHEMA:
        raise ValueError(f"{path}: unknown truth schema {t.get('schema')!r}")
    return t


# ================================================================================================ runners
class Stopwatch:
    def __init__(self) -> None:
        self.stages: dict[str, float] = {}

    def run(self, stage: str, fn, *a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            self.stages[stage] = round(self.stages.get(stage, 0.0) + time.perf_counter() - t0, 3)


def rizin_backend(cfg: dict):
    from rebuild_controller.backends.rizin_worker import RizinBackend
    from rebuild_controller.config import Settings
    b = RizinBackend(Settings(), analysis=dict(cfg["analysis"]))
    tool = b.tool()
    if tool is None or not tool.version:
        b.close()
        return None, "rizin not found (set REBUILD_STUDIO_TOOLS or install the pinned rizin)"
    return b, None


def run_native(binary: Path, truth: dict | None, cfg: dict, backend) -> dict:
    sw = Stopwatch()
    pk_cfg = cfg.get("packer") or {}
    packer_info = None
    with tempfile.TemporaryDirectory(prefix="rs-bench-unpack-") as work:
        if pk_cfg.get("check", True):
            from rebuild_controller.backends import packer
            binary, packer_info = sw.run("packer", packer.prepare_for_analysis, binary, Path(work),
                                         allow_unpack=bool(pk_cfg.get("unpack", True)), tools_dir=backend.settings.tools_dir)
        out = _run_native(binary, truth, cfg, backend, sw)
        gcfg = cfg.get("ghidra") or {}
        if truth is not None and gcfg.get("enabled") and truth.get("row") not in (gcfg.get("skip_rows") or []):
            out["ghidra"] = sw.run("ghidra", run_ghidra, binary, truth, gcfg)
        if packer_info and (packer_info.get("unpack") or {}).get("ok"):
            backend.pool.close_all()   # release the unpacked copy before its temp folder is removed (Windows file locks)
    if truth is not None:
        rep = (packer_info or {}).get("report") or {}
        unp = (packer_info or {}).get("unpack") or {}
        out["packed"] = {"truth": bool(truth.get("packed")), "detected": rep.get("packed") if packer_info else None,
                         "packer": rep.get("packer"), "unpacked": bool(unp.get("ok")),
                         "unpack_tool": f"upx {unp.get('tool_version')}" if unp.get("ok") else None,
                         "note": unp.get("reason") or (rep.get("summary") if packer_info else "packer check disabled by config")}
    return out


def _run_native(binary: Path, truth: dict | None, cfg: dict, backend, sw: "Stopwatch") -> dict:
    sess = backend.session_for_path(binary)
    caps = sess.capabilities()
    analysis = sw.run("analysis", sess.analyze)
    funcs, ftrunc = sw.run("functions", sess.functions)
    out: dict[str, Any] = {"status": "ok", "analysis": {"settings": analysis.get("settings"), "possibly_partial": analysis.get("possibly_partial")},
                           "decompiler": sess.decompiler_id(), "rz_ghidra_loaded": caps["rz_ghidra_loaded"],
                           "functions_found": len(funcs), "functions_truncated": ftrunc}
    found_imports: list[dict] = []
    if cfg.get("imports", True):
        imp, _ = sw.run("imports", sess.imports)
        found_imports = [i for i in imp or [] if isinstance(i, dict)]
    found_strings: list[str] = []
    if cfg.get("strings", True):
        st, _ = sw.run("strings", sess.strings)
        found_strings = [s.get("string", "") for s in st or [] if isinstance(s, dict)]
    out["imports_found"] = len(found_imports)
    out["strings_found"] = len(found_strings)
    if truth is not None:
        ranges = [(as_int(a), as_int(b)) for a, b in truth.get("code_ranges", [])]
        out["functions"] = score_functions(truth["functions"], funcs, ranges)
        out["imports"] = score_imports(truth["imports"], found_imports) if cfg.get("imports", True) and truth["imports"] else None
        out["strings"] = score_strings(truth["strings"], found_strings) if cfg.get("strings", True) else None
        out["decompile"] = sw.run("decompile", decompile_own, sess, truth, funcs, int(cfg["decompile"].get("max_functions", 200)))
    out["seconds"] = sw.stages
    return out


def run_ghidra(binary: Path, truth: dict, gcfg: dict) -> dict:
    """Ghidra headless whole-program run: function-boundary recall/precision of Ghidra's own function list (as far as
    max_functions reaches) and real-decompiler coverage of our own source functions."""
    from rebuild_controller.backends.ghidra import GhidraBackend
    from rebuild_controller.config import Settings
    g = GhidraBackend(Settings())
    probe = g.tool_probe()
    if probe.version is None or probe.availability.value not in ("installed", "usable", "verified"):
        return {"status": f"not run: {probe.detail}"}
    try:
        res = g.decompile_all_path(binary, max_functions=int(gcfg.get("max_functions", 2000)),
                                   per_function_timeout=int(gcfg.get("per_function_timeout", 60)), timeout=3600)
    except Exception as e:
        return {"status": f"error: {type(e).__name__}: {str(e)[:200]}"}
    funcs = [{"offset": as_int(f["entry"]), "name": f.get("name"), "size": f.get("size")} for f in res["functions"]]
    ranges = [(as_int(a), as_int(b)) for a, b in truth.get("code_ranges", [])]
    fs = score_functions(truth["functions"], funcs, ranges)
    ok = {as_int(f["entry"]) for f in res["functions"] if f.get("ok") and (f.get("code") or "").strip()}
    own = [as_int(f["start"]) for f in truth["functions"] if f.get("own")]
    complete = res["total_functions"] is not None and len(res["functions"]) >= res["total_functions"]
    return {"status": "ok", "ghidra_version": res["ghidra_version"], "functions_total": res["total_functions"],
            "decompiled": res["decompiled"], "failed": res["failed"], "list_complete": complete,
            "recall": fs["recall"], "precision": fs["precision"], "own_coverage": ratio(sum(1 for a in own if a in ok), len(own)),
            "own_real": sum(1 for a in own if a in ok), "own_functions": len(own), "seconds": res["seconds"]}


def decompile_own(sess, truth: dict, funcs: list[dict], limit: int) -> dict:
    own = sorted((f for f in truth["functions"] if f.get("own")), key=lambda f: as_int(f["start"]))
    found = {as_int(f.get("offset")) for f in funcs}
    res = {"own_functions": len(own), "attempted": 0, "real": 0, "pseudo_only": 0, "no_function": 0, "errors": 0,
           "not_attempted_budget": 0, "failures": []}
    for f in own:
        start = as_int(f["start"])
        if start not in found:
            res["no_function"] += 1
            res["failures"].append({"function": f["name"], "reason": "analyzer found no function at this start"})
            continue
        if res["attempted"] >= limit:
            res["not_attempted_budget"] += 1
            continue
        res["attempted"] += 1
        try:
            d = sess.decompile(start)
        except Exception as e:  # a decompiler crash/timeout is a measured failure, not a benchmark error
            res["errors"] += 1
            res["failures"].append({"function": f["name"], "reason": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        if d.get("is_real_decompiler") and (d.get("text") or "").strip():
            res["real"] += 1
        else:
            res["pseudo_only"] += 1
            res["failures"].append({"function": f["name"], "reason": f"no real decompiler output ({d.get('decompiler')})"})
    res["coverage"] = ratio(res["real"], len(own))
    res["failures"] = res["failures"][:20]
    return res


def ilspy_backend():
    from rebuild_controller.backends.ilspy import ILSpyBackend
    from rebuild_controller.config import Settings
    cands = []
    if os.environ.get("REBUILD_STUDIO_TOOLS"):
        cands.append(Path(os.environ["REBUILD_STUDIO_TOOLS"]))
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "RebuildStudio" / "tools")
    for d in cands:
        b = ILSpyBackend(Settings(tools_dir=d))
        exe, ver = b._tool_version()
        if exe is not None and ver:
            return b, f"ilspycmd {ver} ({exe})"
    b = ILSpyBackend(Settings())
    exe, ver = b._tool_version()
    if exe is not None and ver:
        return b, f"ilspycmd {ver} ({exe})"
    return None, "ilspycmd not found in REBUILD_STUDIO_TOOLS, %LOCALAPPDATA%/RebuildStudio/tools or PATH"


def run_dotnet(binary: Path, truth: dict | None, cfg: dict, backend) -> dict:
    sw = Stopwatch()
    with tempfile.TemporaryDirectory(prefix="rs-bench-net-") as tmp:
        r = sw.run("decompile", backend.decompile, binary, Path(tmp) / "out", timeout=float(cfg["dotnet"].get("timeout", 600)))
        if not r.ok:
            return {"status": f"failed: {r.error}", "seconds": sw.stages}
        rep = r.data["recovery_report"]
        texts = [p.read_text(encoding="utf-8", errors="replace") for p in sorted((Path(tmp) / "out").rglob("*.cs"))]
    csharp = "\n".join(texts)
    if truth is None:
        sys.path.insert(0, str(BENCH))
        from build_bench import truth_dotnet
        dn = truth_dotnet(binary)
        strings: list[str] = []
    else:
        dn, strings = truth["dotnet"], truth["strings"]
    out = {"status": "ok", "dotnet": score_dotnet(dn, csharp),
           "decompile": {"types_total": rep["types_total"], "types_decompiled": rep["types_decompiled"],
                         "types_with_errors": rep["types_with_errors"], "types_failed": rep["types_failed"],
                         "error_markers": rep["error_markers"], "mode": rep["mode"],
                         "coverage": ratio(rep["types_decompiled"] + rep["types_with_errors"], rep["types_total"]),
                         "failures": rep["types_failed"] + rep["error_markers"]},
           "seconds": sw.stages}
    if strings:
        out["strings"] = score_strings(strings, [csharp])
    return out


# ================================================================================================ native-language rebuild (R4)
NATIVE_REBUILD_ORACLES = {"dotnetapp": "fixtures/dotnetapp/expected/scenarios.json", "javacli": "fixtures/javacli/expected/scenarios.json"}
NATIVE_REBUILD_TARGET = {"dotnet": "csharp", "jvm": "java"}
_TOOL_DIRS = ("ilspycmd", "dotnet", "dotnet-sdk", "cfr", "jre", "jdk21")


def _tool_roots() -> list[Path]:
    roots = []
    if os.environ.get("REBUILD_STUDIO_TOOLS"):
        roots.append(Path(os.environ["REBUILD_STUDIO_TOOLS"]))
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "RebuildStudio" / "tools")
    return [r for r in roots if r.is_dir()]


def merged_tools_dir(dest: Path) -> tuple[Path, list[Path]]:
    """One tools folder for the pipeline (ILSpy, private .NET runtime/SDK, CFR, JRE, JDK 21) when they live in different tool
    folders: directory junctions on Windows (read-only use, removed afterwards), else the first tools folder."""
    roots = _tool_roots()
    if os.name != "nt" or not roots:
        return (roots[0] if roots else dest), []
    import _winapi
    dest.mkdir(parents=True, exist_ok=True)
    links = []
    for name in _TOOL_DIRS:
        for r in roots:
            if (r / name).is_dir():
                _winapi.CreateJunction(str(r / name), str(dest / name))
                links.append(dest / name)
                break
    return dest, links


def run_native_rebuild(row: str, kind: str, source_root: Path, cfg: dict) -> dict:
    """Run the product pipeline (inventory -> recovery -> native_rebuild -> deliver) with AI off; report what the verifier said."""
    from rebuild_controller.config import Limits, Settings, set_settings
    from rebuild_controller.jobs import JobState
    from rebuild_controller.services import StudioServices
    target = NATIVE_REBUILD_TARGET[kind]
    oracle = REPO / NATIVE_REBUILD_ORACLES[row] if row in NATIVE_REBUILD_ORACLES else None
    timeout = int((cfg.get("native_rebuild") or {}).get("timeout", 900))
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="rs-bench-r4-", ignore_cleanup_errors=True) as tmp:
        tools, links = merged_tools_dir(Path(tmp) / "tools")
        try:
            s = Settings(data_dir=Path(tmp) / "data", limits=Limits(lease_timeout_seconds=300, worker_heartbeat_seconds=0, max_stage_seconds=timeout))
            s.tools_dir = tools
            s.ensure_dirs()
            set_settings(s)
            st = StudioServices(s)
            try:
                case = st.create_case(name=f"bench-{row}", source_root=str(source_root), output_root=str(Path(tmp) / "out"), target_language=target,
                                      output_type="exe", ai_policy={"mode": "no_ai"},
                                      launch_profile={"baseline_file": str(oracle)} if oracle else {})
                cid = case["case_id"]
                st.start_rebuild(cid)
                for _ in range(400):
                    n = st.runner.run_pending()
                    if n == 0 and not st.jobs.list(None, [JobState.QUEUED, JobState.RUNNING]):
                        break
                jobs = {j.stage: j for j in st.jobs.list(cid)}
                nr = jobs.get("native_rebuild")
                if nr is None or nr.state != JobState.COMPLETED:
                    bad = next((j for j in st.jobs.list(cid) if j.state != JobState.COMPLETED), None)
                    why = (nr.error if nr else (bad.error if bad else "no native_rebuild job")) or "not completed"
                    return {"status": f"failed: {str(why).splitlines()[0][:200]}", "target": target, "seconds": round(time.perf_counter() - t0, 1)}
                r = nr.result or {}
                cand = st.candidates.get(r["final_candidate"])
                build = (cand.get("meta") or {}).get("build") or {}
                return {"status": "ok", "target": target, "built": bool(r.get("built")), "verified": bool(r.get("verified")),
                        "scenarios": r.get("scenarios"), "passed": r.get("passed"), "oracle": bool(oracle),
                        "repairs": len(r.get("deterministic_repairs") or []), "repair_notes": r.get("deterministic_repairs") or [],
                        "rounds": len(r.get("rounds") or []), "ai_used": False, "toolchain": build.get("toolchain"),
                        "seconds": round(time.perf_counter() - t0, 1)}
            finally:
                st.stop()
        finally:
            for ln in links:
                try:
                    os.rmdir(ln)          # removes the junction only, never the tool folder it points to
                except OSError:
                    pass


def native_rebuild_cell(r: dict) -> str:
    nr = r.get("native_rebuild")
    if str(r.get("kind", "")).startswith("native"):
        return "n/a (no original-language source; Rust port)"
    if not isinstance(nr, dict):
        return "not run"
    if nr.get("status") != "ok":
        return str(nr.get("status", "not run"))
    lang = {"csharp": "C#", "java": "Java"}.get(nr["target"], nr["target"])
    if not nr["built"]:
        return f"{lang}: does not build ({nr['repairs']} fixes)"
    sc = f", {nr['passed']}/{nr['scenarios']} scenarios" if nr.get("oracle") and nr.get("scenarios") else ", no scenario oracle"
    return f"{lang}: builds{sc}, {nr['repairs']} fixes, no AI"


def _native_rebuild_safe(row: dict, cfg: dict) -> dict:
    try:
        out = run_native_rebuild(row["row"], row["kind"], Path(row["binary"]).parent, cfg)
    except Exception as e:  # noqa: BLE001 - reported, never a silent pass
        out = {"status": f"error: {type(e).__name__}: {str(e)[:200]}"}
    print(f"[benchmark] {row['row']}: native-language rebuild: {native_rebuild_cell({'kind': row['kind'], 'native_rebuild': out})}", file=sys.stderr)
    return out


# ================================================================================================ driver
def discover_rows(selected: list[str] | None, legacy: bool) -> list[dict]:
    rows = []
    man = BENCH / "manifest.json"
    doc = json.loads(man.read_text(encoding="utf-8")) if man.is_file() else {"rows": {}, "not_built": {}}
    for name, r in doc.get("rows", {}).items():
        rows.append({"row": name, "kind": r["kind"], "binary": BENCH / r["binary"], "truth": BENCH / r["truth"], "origin": "bench"})
    for name, why in doc.get("not_built", {}).items():
        rows.append({"row": name, "kind": "?", "not_run": why if why.startswith("not run") else f"not run: {why}", "origin": "bench"})
    if legacy:
        for name, r in LEGACY.items():
            e = {"row": name, "kind": r["kind"], "origin": "fixture"}
            if "not_scored" in r:
                e["not_run"] = r["not_scored"]
                if r.get("binary"):
                    e["binary"] = REPO / r["binary"]
            else:
                e.update(binary=REPO / r["binary"], truth=None, partial=r["why_partial"])
            rows.append(e)
    if selected:
        rows = [r for r in rows if r["row"] in selected]
    return rows


def run_benchmark(rows: list[dict], cfg: dict) -> dict:
    results = []
    rz, rz_err = (None, None)
    net, net_info = (None, None)
    env: dict[str, Any] = {}
    try:
        for row in rows:
            res: dict[str, Any] = {"row": row["row"], "kind": row["kind"], "origin": row["origin"]}
            if row.get("partial"):
                res["partial"] = row["partial"]
            if "not_run" in row:
                res["status"] = row["not_run"]
                if row["kind"] in NATIVE_REBUILD_TARGET and row.get("binary") and (cfg.get("native_rebuild") or {}).get("enabled", True):
                    res["native_rebuild"] = _native_rebuild_safe(row, cfg)
                results.append(res)
                continue
            if not row["binary"].is_file():
                res["status"] = f"not run: binary missing ({row['binary']})"
                results.append(res)
                continue
            truth = load_truth(row["truth"]) if row.get("truth") else None
            if truth:
                res["truth_source"] = truth.get("truth_source")
            t0 = time.perf_counter()
            try:
                if row["kind"].startswith("native"):
                    if rz is None and rz_err is None:
                        rz, rz_err = rizin_backend(cfg)
                        if rz is not None:
                            tool = rz.tool()
                            env["rizin"] = {"version": tool.version, "commit": tool.commit, "exe": str(tool.exe),
                                            "rz_ghidra_plugin": str(tool.ghidra_plugin) if tool.ghidra_plugin else None}
                    if rz is None:
                        res["status"] = f"not run: {rz_err}"
                    else:
                        res.update(run_native(row["binary"], truth, cfg, rz))
                elif row["kind"] == "dotnet":
                    if not cfg.get("dotnet", {}).get("enabled", True):
                        res["status"] = "not run: dotnet disabled by config"
                    else:
                        if net is None and net_info is None:
                            net, net_info = ilspy_backend()
                            env["ilspy"] = net_info
                        res.update(run_dotnet(row["binary"], truth, cfg, net) if net else {"status": f"not run: {net_info}"})
                    if (cfg.get("native_rebuild") or {}).get("enabled", True):
                        res["native_rebuild"] = _native_rebuild_safe(row, cfg)
                else:
                    res["status"] = f"not run: no scorer for kind {row['kind']}"
            except Exception as e:  # report and continue with the other rows
                res["status"] = f"error: {type(e).__name__}: {str(e)[:300]}"
            res["wall_seconds"] = round(time.perf_counter() - t0, 3)
            results.append(res)
            print(f"[benchmark] {row['row']}: {res.get('status')} ({res['wall_seconds']}s)", file=sys.stderr)
    finally:
        if rz is not None:
            rz.close()
    return {"schema": REPORT_SCHEMA, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "config": cfg,
            "environment": {**env, "python": sys.version.split()[0], "platform": sys.platform}, "rows": results}


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _packed_cell(packed: dict) -> str:
    det = packed.get("detected")
    if det is None:
        return "missed (no packer check)" if packed.get("truth") else "-"
    if packed.get("truth"):
        cell = f"yes ({packed.get('packer')})" if det else "missed"
        return cell + (f", unpacked with {packed['unpack_tool']}" if packed.get("unpacked") else "")
    return "false positive" if det else "-"


def render_markdown(rep: dict) -> str:
    cfg = rep["config"]
    L = ["# Benchmark scoreboard (R0 corpus)", "",
         f"Generated {rep['generated_utc']} by `scripts/benchmark.py` with config **{cfg['name']}** "
         f"(analysis `{cfg['analysis'].get('command')}` + passes `{','.join(cfg['analysis'].get('passes') or []) or 'none'}`, "
         f"timeout {cfg['analysis'].get('analysis.timeout')} s, decompile budget {cfg['decompile'].get('max_functions')} own functions, "
         f"packer check {'on' if (cfg.get('packer') or {}).get('check', True) else 'off'}).", ""]
    envd = rep["environment"]
    if envd.get("rizin"):
        L.append(f"Native engine: rizin {envd['rizin']['version']} (commit {str(envd['rizin']['commit'])[:12]}), "
                 f"rz-ghidra plugin: {'loaded' if envd['rizin'].get('rz_ghidra_plugin') else 'absent'}. ")
    if envd.get("ilspy"):
        L.append(f".NET engine: {envd['ilspy']}.")
    L += ["", "Ground truth: our own sources built with symbols (PDB / Go symbol table / unobfuscated metadata); see "
          "`fixtures/bench/README.md`. Function boundaries are matched by exact start address. Precision counts only functions "
          "found inside executable sections. Decompiler coverage = own source functions with real rz-ghidra output / all own "
          "source functions in the truth.", "",
          "## Native rows", "",
          "| Row | Truth fns | Found | Boundary recall | Boundary precision | Own-fn recall | Named recall | Name precision | Real-decompiler coverage (own) | Decompile failures | Imports recall | Strings recall | Packed detected | Analysis s | Decompile s | Total s | Native-language rebuild |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        if not r["kind"].startswith("native") or r.get("status") != "ok":
            continue
        f = r.get("functions")
        d = r.get("decompile") or {}
        s = r.get("seconds", {})
        if f is None:
            L.append(f"| {r['row']} (partial) | n/a | {r['functions_found']} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | "
                     f"{s.get('analysis', 0):.1f} | - | {r['wall_seconds']:.1f} | {native_rebuild_cell(r)} |")
            continue
        packed = r.get("packed", {})
        pk = _packed_cell(packed)
        fails = d.get("errors", 0) + d.get("pseudo_only", 0) + d.get("no_function", 0)
        L.append(f"| {r['row']} | {f['truth_functions']} | {f['found_functions']} | {_pct(f['recall'])} | {_pct(f['precision'])} | "
                 f"{_pct(f['own_recall'])} | {_pct(f['named_recall'])} | {_pct(f.get('named_precision'))} | {_pct(d.get('coverage'))} ({d.get('real', 0)}/{d.get('own_functions', 0)}) | "
                 f"{fails} | {_pct((r.get('imports') or {}).get('recall'))} | {_pct((r.get('strings') or {}).get('recall'))} | {pk} | "
                 f"{s.get('analysis', 0):.1f} | {s.get('decompile', 0):.1f} | {r['wall_seconds']:.1f} | {native_rebuild_cell(r)} |")
    gh = [r for r in rep["rows"] if r.get("status") == "ok" and isinstance(r.get("ghidra"), dict)]
    if gh:
        L += ["", "## Ghidra headless (optional second decompiler)", "",
              "Whole-program `analyzeHeadless` run per row (auto-analysis, then every function decompiled, largest first). "
              "Boundary recall/precision use Ghidra's own function list.", "",
              "| Row | Ghidra | Functions | Boundary recall | Boundary precision | Real-decompiler coverage (own) | Decompile failures | Seconds |",
              "|---|---|---|---|---|---|---|---|"]
        for r in gh:
            g = r["ghidra"]
            if g.get("status") != "ok":
                L.append(f"| {r['row']} | {g.get('status')} | | | | | | |")
                continue
            L.append(f"| {r['row']} | {g['ghidra_version']} | {g['functions_total']} | {_pct(g['recall'])} | {_pct(g['precision'])} | "
                     f"{_pct(g['own_coverage'])} ({g['own_real']}/{g['own_functions']}) | {g['failed']} | {g['seconds']:.1f} |")
    L += ["", "## .NET rows", "",
          "| Row | Truth types | Type recall | Truth methods | Method recall | Types decompiled | Decompile failures | Strings recall | Decompile s | Native-language rebuild |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        if r["kind"] != "dotnet" or r.get("status") != "ok":
            continue
        dn, d = r["dotnet"], r["decompile"]
        L.append(f"| {r['row']}{' (partial)' if r.get('partial') else ''} | {dn['truth_types']} | {_pct(dn['type_recall'])} | {dn['truth_methods']} | "
                 f"{_pct(dn['method_recall'])} | {_pct(d['coverage'])} ({d['types_decompiled'] + d['types_with_errors']}/{d['types_total']}) | "
                 f"{d['failures']} | {_pct((r.get('strings') or {}).get('recall'))} | {r['seconds'].get('decompile', 0):.1f} | {native_rebuild_cell(r)} |")
    nrows = [r for r in rep["rows"] if isinstance(r.get("native_rebuild"), dict)]
    if nrows:
        L += ["", "## Native-language rebuild (R4)", "",
              "The product pipeline with AI off: the recovered C# (ILSpy) / Java (CFR) is the candidate, built with the private .NET SDK / "
              "JDK in the sandbox after deterministic fixes, then run against the fixture's frozen scenario oracle where there is one. "
              "Only the verifier's verdict counts."
              + (f" Measured {rep['native_rebuild_generated_utc']}." if rep.get("native_rebuild_generated_utc") else ""), "",
              "| Row | Target | Builds | Scenarios matched | Deterministic fixes | AI | Toolchain | Seconds |", "|---|---|---|---|---|---|---|---|"]
        for r in nrows:
            nr = r["native_rebuild"]
            if nr.get("status") != "ok":
                L.append(f"| {r['row']} | {nr.get('target', '')} | {nr.get('status')} | | | | | |")
                continue
            sc = f"{nr['passed']}/{nr['scenarios']}" if nr.get("oracle") and nr.get("scenarios") is not None else "no oracle"
            L.append(f"| {r['row']} | {nr['target']} | {'yes' if nr['built'] else 'no'} | {sc} | {nr['repairs']} | {'yes' if nr.get('ai_used') else 'none'} | "
                     f"{nr.get('toolchain') or ''} | {nr['seconds']:.1f} |")
        notes = [(r["row"], n) for r in nrows for n in (r["native_rebuild"].get("repair_notes") or [])]
        if notes:
            L += ["", "Deterministic fixes applied:", ""] + [f"* {row}: {n}" for row, n in notes]
    L += ["", "## Rows not run or not scored", "", "| Row | Status |", "|---|---|"]
    for r in rep["rows"]:
        if r.get("status") != "ok":
            L.append(f"| {r['row']} | {r.get('status')}" + ("; native-language rebuild: see that section" if isinstance(r.get("native_rebuild"), dict) else "") + " |")
    partial = [r for r in rep["rows"] if r.get("partial") and r.get("status") == "ok"]
    if partial:
        L += ["", "## Partial rows", ""] + [f"* **{r['row']}**: {r['partial']}" for r in partial]
    L += ["", "## Notes", "",
          "* `Packed detected`: the product's packer check (`backends/packer.py`: section names, UPX magic, entropy, W+X/virtual-only code sections, entry-point and import anomalies). A UPX row is then unpacked with the pinned `upx -d` into a temp work folder (consent = benchmark config `packer.unpack`) and the unpacked copy is what gets scored; `false positive` marks an unpacked row reported as packed.",
          "* Named recall counts a function as named only when rizin's name at the true start equals a truth name (modulo prefixes, case, punctuation); auto names (`fcn.*`, `entry0`) never count. Name precision = right names / matched functions that carry any non-auto name (a wrong name misleads more than `fcn.*`; rizin's RTTI names such as `method.Foo.virtual_0` count as wrong).",
          "* Analysis passes (R1, `backends/rizin_passes.py`): `sigpacks` = FLIRT packs built from the MSVC 14.29 runtime libraries and the Rust 1.98.1 std rlibs (`scripts/build_sigpacks.py`, pinned in `rebuild_controller/data/sigpacks/manifest.json`; never built from this corpus); `pdata` = x64 exception-directory function starts (chained entries and EH funclets skipped); `relocptrs` = functions at relocated code pointers no analysed function covers; `thunks` = `jmp [IAT]` thunks named after their import.",
          "* Failures per row are listed in `reports/benchmark.json` (`decompile.failures`, `imports.missing_sample`, `strings.missing`).",
          "* Reproduce: `python fixtures/bench/build_bench.py --verify` (corpus), then `python scripts/benchmark.py` (needs rizin; REBUILD_STUDIO_TOOLS).",
          "* Native-language rebuild (R4): `python scripts/benchmark.py --only-native-rebuild` re-measures only that column (needs ILSpy + .NET SDK, CFR + JDK) and keeps the other measurements.", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="R0 benchmark scoreboard")
    ap.add_argument("--rows", help="comma-separated row names (default: all)")
    ap.add_argument("--config", type=Path, help="JSON file with analyzer knobs (merged over the defaults)")
    ap.add_argument("--out-dir", type=Path, default=REPO / "reports")
    ap.add_argument("--no-legacy", action="store_true", help="skip the original acceptance fixtures")
    ap.add_argument("--only-native-rebuild", action="store_true",
                    help="keep the other measurements in <out-dir>/benchmark.json and (re)run only the native-language rebuild column")
    a = ap.parse_args(argv)
    override = json.loads(a.config.read_text(encoding="utf-8")) if a.config else None
    cfg = merge_config(DEFAULT_CONFIG, override)
    rows = discover_rows([r for r in a.rows.split(",") if r] if a.rows else None, not a.no_legacy)
    if a.only_native_rebuild:
        rep = json.loads((a.out_dir / "benchmark.json").read_text(encoding="utf-8"))
        by = {r["row"]: r for r in rows}
        for r in rep["rows"]:
            src = by.get(r["row"])
            if src and src["kind"] in NATIVE_REBUILD_TARGET and src.get("binary"):
                r["native_rebuild"] = _native_rebuild_safe(src, cfg)
        rep.setdefault("config", {})["native_rebuild"] = cfg["native_rebuild"]
        rep["native_rebuild_generated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    else:
        rep = run_benchmark(rows, cfg)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "benchmark.json").write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8", newline="\n")
    (a.out_dir / "benchmark.md").write_text(render_markdown(rep), encoding="utf-8", newline="\n")
    print(f"wrote {a.out_dir / 'benchmark.json'} and benchmark.md ({len(rep['rows'])} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
