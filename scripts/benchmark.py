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

usage: python scripts/benchmark.py [--rows a,b] [--config cfg.json] [--out-dir reports] [--no-legacy]
Config keys (all optional; defaults in DEFAULT_CONFIG):
  {"name": "default", "analysis": {"command": "aaa", "analysis.timeout": 300},
   "decompile": {"max_functions": 200}, "strings": true, "imports": true, "dotnet": {"enabled": true, "timeout": 600}}
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
    "analysis": {"command": "aaa", "analysis.timeout": 300},
    "decompile": {"max_functions": 200},
    "strings": True,
    "imports": True,
    "dotnet": {"enabled": True, "timeout": 600},
}
# A deliberately weaker analyzer: shallow analysis (no call-target recursion) and no decompilation. Used by the guard test.
WEAK_CONFIG: dict[str, Any] = {"name": "weak", "analysis": {"command": "aa", "analysis.timeout": 300}, "decompile": {"max_functions": 0}}

LEGACY = {
    "pecli": {"kind": "native_pe_x64", "binary": "fixtures/pecli/original/pecli.exe",
              "why_partial": "no symbol map: shipped stripped (mingw -O1 -s) and mingw is not on this host to rebuild an unstripped twin; "
                             "only functions-found / imports / strings found are reported"},
    "dotnetapp": {"kind": "dotnet", "binary": "fixtures/dotnetapp/original/dotnetapp.dll",
                  "why_partial": "names are not obfuscated, so the assembly's own metadata is the type/method truth"},
    "javacli": {"kind": "jvm", "not_scored": "not scored by R0 metrics: JVM jar (CFR path); parity is measured by its scenario oracle"},
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
    sized = [s for s in matched if truth[s].get("size")]
    size_exact = [s for s in sized if as_int(found_by_start[s].get("size")) == truth[s]["size"]]
    return {
        "truth_functions": len(truth), "found_functions": len(found_by_start), "found_in_code": len(in_code),
        "matched": len(matched), "recall": ratio(len(matched), len(truth)), "precision": ratio(len(matched), len(in_code)),
        "own_truth": len(own), "own_matched": len(own_matched), "own_recall": ratio(len(own_matched), len(own)),
        "named": len(named), "named_recall": ratio(len(named), len(truth)),
        "own_named_recall": ratio(sum(1 for s in own if s in named), len(own)),
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
        out["packed"] = {"truth": bool(truth.get("packed")), "detected": None,
                         "note": "the analysis pipeline has no packer/entropy check yet (R1); a packed row counts as missed"}
    out["seconds"] = sw.stages
    return out


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


def render_markdown(rep: dict) -> str:
    cfg = rep["config"]
    L = ["# Benchmark scoreboard (R0)", "",
         f"Generated {rep['generated_utc']} by `scripts/benchmark.py` with config **{cfg['name']}** "
         f"(analysis `{cfg['analysis'].get('command')}`, timeout {cfg['analysis'].get('analysis.timeout')} s, "
         f"decompile budget {cfg['decompile'].get('max_functions')} own functions).", ""]
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
          "| Row | Truth fns | Found | Boundary recall | Boundary precision | Own-fn recall | Named recall | Real-decompiler coverage (own) | Decompile failures | Imports recall | Strings recall | Packed detected | Analysis s | Decompile s | Total s |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        if not r["kind"].startswith("native") or r.get("status") != "ok":
            continue
        f = r.get("functions")
        d = r.get("decompile") or {}
        s = r.get("seconds", {})
        if f is None:
            L.append(f"| {r['row']} (partial) | n/a | {r['functions_found']} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | "
                     f"{s.get('analysis', 0):.1f} | - | {r['wall_seconds']:.1f} |")
            continue
        packed = r.get("packed", {})
        pk = ("missed (no packer check)" if packed.get("detected") is None else ("yes" if packed["detected"] else "no")) if packed.get("truth") else "-"
        fails = d.get("errors", 0) + d.get("pseudo_only", 0) + d.get("no_function", 0)
        L.append(f"| {r['row']} | {f['truth_functions']} | {f['found_functions']} | {_pct(f['recall'])} | {_pct(f['precision'])} | "
                 f"{_pct(f['own_recall'])} | {_pct(f['named_recall'])} | {_pct(d.get('coverage'))} ({d.get('real', 0)}/{d.get('own_functions', 0)}) | "
                 f"{fails} | {_pct((r.get('imports') or {}).get('recall'))} | {_pct((r.get('strings') or {}).get('recall'))} | {pk} | "
                 f"{s.get('analysis', 0):.1f} | {s.get('decompile', 0):.1f} | {r['wall_seconds']:.1f} |")
    L += ["", "## .NET rows", "",
          "| Row | Truth types | Type recall | Truth methods | Method recall | Types decompiled | Decompile failures | Strings recall | Decompile s |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        if r["kind"] != "dotnet" or r.get("status") != "ok":
            continue
        dn, d = r["dotnet"], r["decompile"]
        L.append(f"| {r['row']}{' (partial)' if r.get('partial') else ''} | {dn['truth_types']} | {_pct(dn['type_recall'])} | {dn['truth_methods']} | "
                 f"{_pct(dn['method_recall'])} | {_pct(d['coverage'])} ({d['types_decompiled'] + d['types_with_errors']}/{d['types_total']}) | "
                 f"{d['failures']} | {_pct((r.get('strings') or {}).get('recall'))} | {r['seconds'].get('decompile', 0):.1f} |")
    L += ["", "## Rows not run or not scored", "", "| Row | Status |", "|---|---|"]
    for r in rep["rows"]:
        if r.get("status") != "ok":
            L.append(f"| {r['row']} | {r.get('status')} |")
    partial = [r for r in rep["rows"] if r.get("partial") and r.get("status") == "ok"]
    if partial:
        L += ["", "## Partial rows", ""] + [f"* **{r['row']}**: {r['partial']}" for r in partial]
    L += ["", "## Notes", "",
          "* `Packed detected`: the pipeline has no packer/entropy check yet, so a packed row is reported as missed until R1 adds one.",
          "* Named recall counts a function as named only when rizin's name at the true start equals a truth name (modulo prefixes, case, punctuation); auto names (`fcn.*`, `entry0`) never count.",
          "* Failures per row are listed in `reports/benchmark.json` (`decompile.failures`, `imports.missing_sample`, `strings.missing`).",
          "* Reproduce: `python fixtures/bench/build_bench.py --verify` (corpus), then `python scripts/benchmark.py` (needs rizin; REBUILD_STUDIO_TOOLS).", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="R0 benchmark scoreboard")
    ap.add_argument("--rows", help="comma-separated row names (default: all)")
    ap.add_argument("--config", type=Path, help="JSON file with analyzer knobs (merged over the defaults)")
    ap.add_argument("--out-dir", type=Path, default=REPO / "reports")
    ap.add_argument("--no-legacy", action="store_true", help="skip the original acceptance fixtures")
    a = ap.parse_args(argv)
    override = json.loads(a.config.read_text(encoding="utf-8")) if a.config else None
    cfg = merge_config(DEFAULT_CONFIG, override)
    rows = discover_rows([r for r in a.rows.split(",") if r] if a.rows else None, not a.no_legacy)
    rep = run_benchmark(rows, cfg)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "benchmark.json").write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8", newline="\n")
    (a.out_dir / "benchmark.md").write_text(render_markdown(rep), encoding="utf-8", newline="\n")
    print(f"wrote {a.out_dir / 'benchmark.json'} and benchmark.md ({len(rep['rows'])} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
