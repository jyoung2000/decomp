#!/usr/bin/env python3
"""Generate reports/final-report.{md,json} from recorded test runs, live doctor output, fixture manifests and demo evidence.

Nothing here is invented: test counts come from reports/test-runs.json (appended by scripts/record_test_run.py from real pytest
output), backend states from `rebuildctl doctor --json` run now, demo outcomes from examples/*/evidence/parity-report.json.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = os.environ.get("REBUILD_PY", "/opt/rebuild-tools/venv/bin/python")
sys.path.insert(0, str(ROOT / "controller"))
from rebuild_controller.outcome import derive_outcome  # noqa: E402  (same derivation the app uses; reads recorded evidence only)

# Example directory -> fixture directory (the fixture holds the declared oracle: features.json + expected/*).
FIXTURE_OF = {"dotnetapp-rust-from-evidence": "dotnetapp", "pecli-rust-from-evidence": "pecli", "webapp-pwa-port": "webapp", "godotgame-bevy-scaffold": "godotgame"}


def sh(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, timeout=300).stdout.strip()
    except Exception as e:  # pragma: no cover
        return f"<error {e}>"


def pins() -> dict:
    out: dict = {}
    pp = tomllib.loads((ROOT / "controller" / "pyproject.toml").read_text())
    out["controller_python_deps"] = pp["project"]["dependencies"]
    ui = json.loads((ROOT / "ui" / "package.json").read_text())
    out["ui_deps"] = {**ui.get("dependencies", {}), **{k: v for k, v in ui.get("devDependencies", {}).items() if k in ("vite", "@playwright/test", "vitest", "typescript")}}
    cargo = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text()
    out["tauri_crates"] = dict(re.findall(r'^(tauri[\w-]*)\s*=\s*\{?\s*version\s*=\s*"([^"]+)"', cargo, re.M)) or dict(re.findall(r'^(tauri[\w-]*)\s*=\s*"([^"]+)"', cargo, re.M))
    lock = ROOT / "docs" / "dependency-lock.json"
    out["dependency_lock"] = json.loads(lock.read_text()) if lock.exists() else None
    out["host_tools"] = {"rizin": sh(["/opt/rebuild-tools/rizin-src-install/bin/rizin", "-v"]).splitlines()[:1], "wine": sh(["wine", "--version"]), "dotnet": sh(["dotnet", "--version"]),
                         "cargo": sh(["cargo", "--version"]), "node": sh(["node", "--version"]), "python": sys.version.split()[0]}
    bm = Path("/opt/rebuild-tools/rizin-src-install/share/rebuild-studio/build-manifest.json")
    out["rizin_build_manifest"] = json.loads(bm.read_text()) if bm.exists() else None
    return out


def doctor() -> dict:
    """Live doctor when the tool venv exists; otherwise the last recorded run (reports/doctor-linux.json), labelled as recorded."""
    if Path(PY).exists():
        raw = sh([PY, "-m", "rebuild_controller.cli.main", "doctor", "--json"], cwd=ROOT / "controller")
        try:
            d = json.loads(raw[raw.find("{"):])
            d["source"] = "live"
            return d
        except Exception:
            pass
    rec = ROOT / "reports" / "doctor-linux.json"
    if rec.exists():
        d = json.loads(rec.read_text(encoding="utf-8"))
        d["source"] = "recorded from reports/doctor-linux.json (the live doctor tools are not present on this host)"
        return d
    return {"source": "none", "error": "no live doctor and no recorded doctor output"}


def test_runs() -> list[dict]:
    p = ROOT / "reports" / "test-runs.json"
    return json.loads(p.read_text()) if p.exists() else []


def _jload(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _scenario_table(comparisons: list[dict], candidate_id: str | None) -> dict[str, dict]:
    """scenario id -> {feature_id, verdict} for one candidate, from the recorded per-channel comparisons (a scenario passes only if every row passed)."""
    out: dict[str, dict] = {}
    for r in comparisons:
        if r.get("candidate_id") != candidate_id:
            continue
        sid = (r.get("details") or {}).get("scenario")
        if sid is None:
            continue
        e = out.setdefault(sid, {"feature_id": r.get("feature_id"), "verdict": "pass"})
        if r.get("verdict") == "error":
            e["verdict"] = "error"
        elif r.get("verdict") != "pass" and e["verdict"] != "error":
            e["verdict"] = "fail"
    return out


def _fixture_counts(fixture: str) -> dict:
    """What the fixture itself declares (independent of any case): feature IDs and oracle scenarios."""
    fdir = ROOT / "fixtures" / fixture
    feats = _jload(fdir / "features.json", [])
    feats = feats if isinstance(feats, list) else feats.get("features", [])
    out: dict = {"features_declared": len(feats), "feature_ids": [f["id"] for f in feats],
                 "features_not_observable_here": [f["id"] for f in feats if f.get("observable_on") in ("windows_only", "electron_runtime_only")]}
    sc = _jload(fdir / "expected" / "scenarios.json")
    if isinstance(sc, dict) and "scenarios" in sc:
        out["oracle_scenarios"] = len(sc["scenarios"])
        per: dict[str, int] = {}
        for x in sc["scenarios"]:
            per[x.get("feature")] = per.get(x.get("feature"), 0) + 1
        out["oracle_scenarios_per_feature"] = per
        out["oracle_scenario_ids_per_feature"] = {k: [x["id"] for x in sc["scenarios"] if x.get("feature") == k] for k in per}
    ws = _jload(fdir / "expected" / "web_scenarios.json")
    if isinstance(ws, dict) and isinstance(ws.get("original"), dict):
        out["oracle_scenarios"] = len(ws["original"].get("scenarios", []))
    return out


def _provenance(example: str, rep: dict, comparisons: list[dict]) -> list[dict]:
    """Who authored each candidate. The stored candidate `author` is 'model' both for an external MCP client and for the app's own AI route,
    so it cannot tell them apart; provenance is taken from the recorded call log / demo client / candidate origin and the basis is stated."""
    edir = ROOT / "examples" / example
    log = (edir / "client-log.txt").read_text(encoding="utf-8", errors="replace") if (edir / "client-log.txt").exists() else ""
    readme = (edir / "README.md").read_text(encoding="utf-8", errors="replace") if (edir / "README.md").exists() else ""
    has_client = (edir / "mcp_client_demo.py").exists()
    final = (rep.get("candidate") or {}).get("candidate_id")
    ids = list(dict.fromkeys([r["candidate_id"] for r in comparisons if r.get("candidate_id")] + ([final] if final else [])))
    n_ai = len(rep.get("ai_usage") or [])
    out = []
    for cid in ids:
        cand = rep.get("candidate") if cid == final else {}
        origin = (cand or {}).get("origin")
        if "propose_candidate" in log and cid in log:
            who, basis = "external MCP client (propose_candidate over the rebuild-mcp stdio server)", f"client-log.txt records the propose_candidate call that created it"
        elif cid == final and has_client and "propose_candidate" in readme:
            who, basis = "external MCP client (propose_candidate over the rebuild-mcp stdio server)", "README.md and mcp_client_demo.py describe the proposal; no call log is kept for this example"
        elif origin == "scaffold":
            who, basis = "controller-authored scaffold (deterministic, no AI)", "candidate origin=scaffold"
        elif cid != final and has_client:
            who, basis = "controller-authored scaffold (deterministic, no AI)", "pipeline scaffold r1 that precedes the external proposal (exits 64, fails every scenario)"
        elif rep["case"]["target_language"] == "web":
            who, basis = "controller-authored deterministic port of the recovered site (no AI)", "README.md: deterministic port; no AI usage recorded"
        else:
            who, basis = "unknown", "no log or metadata identifies the author"
        out.append({"candidate_id": cid, "final": cid == final, "authored_by": who, "basis": basis,
                    "stored_author_field": (cand or {}).get("author", "not recorded in this report") if (cand or {}).get("author", 0) is not None else "null (none recorded)", "app_routed_ai_calls": n_ai})
    return out


def demos() -> list[dict]:
    out = []
    for d in sorted((ROOT / "examples").glob("*/evidence/parity-report.json")):
        rep = json.loads(d.read_text(encoding="utf-8"))
        example = d.parents[1].name
        comps = _jload(d.parent / "comparisons.json", []) or rep.get("comparisons", [])
        cand = rep.get("candidate") or {}
        table = _scenario_table(comps, cand.get("candidate_id"))
        fixture = FIXTURE_OF.get(example, rep["case"]["name"])
        fx = _fixture_counts(fixture)
        ledger = rep.get("features", [])
        ledger_ids = [f["feature_id"] for f in ledger]
        with_scenario = {e["feature_id"] for e in table.values() if e.get("feature_id")}
        jobs = rep.get("jobs") or {}
        outcome = derive_outcome({
            "scenarios_declared": len(table), "scenario_verdicts": [e["verdict"] for e in table.values()], "stale": False,
            "features": [{"id": f["feature_id"], "title": f["title"], "has_scenario": f["feature_id"] in with_scenario} for f in ledger],
            "unsupported_modules": [], "unknown_scope": False,
            "scaffold_only": cand.get("origin") == "scaffold", "built": cand.get("build_status") == "built",
            "delivered": rep["case"].get("status") == "delivered", "recovered": True,
            "pipeline_done": jobs.get("completed", 0), "pipeline_total": sum(jobs.values()) if jobs else 0})
        out.append({"example": example, "fixture": fixture, "case": rep["case"]["name"], "target": rep["case"]["target_language"], "case_status_recorded": rep["case"].get("status"),
                    "parity": rep["parity"], "candidate": cand, "host": rep.get("host"), "ai_usage": rep.get("ai_usage"), "comparisons": len(comps),
                    "outcome": outcome, "scenarios_declared": len(table), "scenarios_passed": sum(1 for e in table.values() if e["verdict"] == "pass"),
                    "scenario_ids": sorted(table), "ledger_feature_ids": ledger_ids, "ledger_features_with_scenario": sorted(with_scenario),
                    "fixture_counts": fx, "declared_in_fixture_not_in_ledger": [f for f in fx["feature_ids"] if f not in ledger_ids],
                    "provenance": _provenance(example, rep, comps)})
    return out


def fixtures() -> dict:
    m = ROOT / "fixtures" / "manifest.json"
    return json.loads(m.read_text()) if m.exists() else {}


def gates() -> list[str]:
    g = ROOT / "docs" / "WINDOWS_RELEASE_GATES.md"
    if not g.exists():
        return ["docs/WINDOWS_RELEASE_GATES.md missing"]
    return [l.strip("# ").strip() for l in g.read_text().splitlines() if re.match(r"^#{2,3}\s*(G|Gate|\d)", l)]


def hermes_gates() -> list[str]:
    h = ROOT / "docs" / "HERMES.md"
    return [l.strip() for l in h.read_text().splitlines() if re.match(r"^\s*[-*]?\s*\*{0,2}G\d", l)][:12] if h.exists() else []


def build() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "host": {"os": platform.platform(), "certifies_windows": False},
        "commits": sh(["git", "log", "--oneline", "-30"]).splitlines(), "branch": sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "pins": pins(), "backends": doctor(), "test_runs": test_runs(), "fixtures": {k: {"kind": v.get("kind"), "oracle": v.get("oracle"), "features": len(v.get("features", []))} for k, v in fixtures().get("fixtures", {}).items()},
        "demos": demos(), "windows_gates": gates(), "hermes_gates": hermes_gates(),
        "limits": [
            "No interactive Windows host in this session: installer/WebView2/UI capture/Hermes desktop/DPAPI are handoffs (docs/WINDOWS_RELEASE_GATES.md).",
            "PE originals were executed under wine; the reports label that runner non-certifying.",
            "No provider API key was supplied: provider adapters are covered by mocked protocol tests only; zero paid calls were made.",
            "Unity IL2CPP, GameMaker, Android/JVM, Unreal profiles are detected but marked experimental/unverified (no backend run).",
            "Scenario results are limited to the scenarios declared for each case. Passing them is not global parity: features with no scenario, Windows-only behaviour and undiscovered scope are not measured.",
            "Godot → Bevy produces a buildable scaffold plus recovered project; gameplay parity is untested because the original cannot run here and no scenarios were declared.",
        ],
    }


def render(r: dict) -> str:
    L = [f"# Rebuild Studio — final report", "", f"Generated {r['generated_at']} on {r['host']['os']} (certifies Windows: {r['host']['certifies_windows']}). Branch `{r['branch']}`.", "",
         "## Implementation commits", ""] + [f"- `{c}`" for c in r["commits"]]
    L += ["", "## Dependency pins", "", f"- Controller: {', '.join(r['pins']['controller_python_deps'])}", f"- UI: {json.dumps(r['pins']['ui_deps'])}", f"- Tauri crates: {json.dumps(r['pins']['tauri_crates'])}",
          f"- Host tools: {json.dumps(r['pins']['host_tools'])}", f"- rizin build manifest: {json.dumps(r['pins']['rizin_build_manifest'])[:600] if r['pins']['rizin_build_manifest'] else 'n/a'}"]
    L += ["", f"## Backend adapters on this host (doctor source: {r['backends'].get('source', 'unknown')})", "", "| Backend | Availability | Tools |", "|---|---|---|"]
    for b in r["backends"].get("backends", []):
        L.append(f"| {b['backend_id']} | {b['availability']} | " + "; ".join(f"{t['name']} {t['availability']} {t.get('version') or ''}" for t in b.get("tools", [])) + " |")
    L += ["", "## Test runs (recorded from real executions)", "", "| Suite | Command | Passed | Failed | Skipped | When |", "|---|---|---|---|---|---|"]
    for t in r["test_runs"]:
        L.append(f"| {t['name']} | `{t['command']}` | {t['passed']} | {t['failed']} | {t['skipped']} | {t['when']} |")
    L += ["", "## Fixtures", ""] + [f"- **{k}**: {v['kind']}, {v['features']} declared features, oracle: {v['oracle']}" for k, v in r["fixtures"].items()]
    L += ["", "## Demonstrations (verifier-decided)", "",
          "Each line separates **pipeline** facts (what was built and published) from **measured behaviour** (declared scenarios compared against the original). "
          "Scenario results cover only the scenarios declared for that case. They are not a claim of global parity: behaviour outside the declared scenarios, "
          "features with no scenario and Windows-only behaviour are not measured.", ""]
    for d in r["demos"]:
        o, v, c = d["outcome"], d["outcome"]["verification"], d["outcome"]["coverage"]
        final = next((x for x in d["provenance"] if x["final"]), None)
        miss = d["declared_in_fixture_not_in_ledger"]
        L.append(f"- **{d['example']}** ({d['case']} -> {d['target']}): **{o['label']}**. Behavior verified: {v['passed']} of {v['declared']} declared scenarios passed, {v['failed']} failed, {v['untested']} not run. "
                 f"Ledger features with no scenario: {c['features_without_oracle']} of {c['features_total']}"
                 f"{(' (' + '; '.join(c['features_without_oracle_titles']) + ')') if c['features_without_oracle_titles'] else ''}. "
                 f"Fixture-declared features not in the ledger: {len(miss)}{(' (' + ', '.join(miss) + ')') if miss else ''}. "
                 f"Recorded comparison rows: {d['comparisons']} across all candidates (rows are per channel per step, not scenario counts). "
                 f"Final candidate author: {final['authored_by'] if final else 'n/a'}. App-routed AI calls: {len(d['ai_usage'] or [])}. "
                 f"Host certifies Windows: {(d['host'] or {}).get('host_certifies_windows', 'n/a')}. Recorded case status: {d['case_status_recorded']}.")
    L += ["", "### What the numbers count (nine scenarios, eight feature IDs)", "",
          "These are different units and must not be mixed up:", "",
          "- **Scenarios** are runnable checks recorded in the fixture oracle (`fixtures/<name>/expected/scenarios.json`). One feature can be checked by more than one scenario.",
          "- **Feature IDs in the ledger** are the semantic features the verifier marks verified or not (`parity-report.json` > `features`). The ledger is filled from the scenarios' `feature` field, so a feature that has no scenario is not in it.",
          "- **Feature IDs declared by the fixture** (`fixtures/<name>/features.json`) also include features that cannot be observed on this Linux host (`windows_only`); they have no scenario.", "",
          "| Example | Scenarios declared / passed | Distinct feature IDs those scenarios cover | Features in the ledger | Features declared by the fixture | Declared by fixture but not in ledger | Scenarios in the full fixture oracle |", "|---|---|---|---|---|---|---|"]
    for d in r["demos"]:
        fx = d["fixture_counts"]
        L.append(f"| {d['example']} | {d['scenarios_declared']} / {d['scenarios_passed']} | {len(d['ledger_features_with_scenario'])} | {len(d['ledger_feature_ids'])} | {fx['features_declared']} | "
                 f"{', '.join(d['declared_in_fixture_not_in_ledger']) or 'none'} | {fx.get('oracle_scenarios', 'n/a')} |")
    for d in r["demos"]:
        per = d["fixture_counts"].get("oracle_scenarios_per_feature") or {}
        multi = {k: n for k, n in per.items() if n > 1}
        if d["example"].startswith("dotnetapp") and multi:
            k = next(iter(multi))
            ids = " and ".join(f"`{x}`" for x in d["fixture_counts"]["oracle_scenario_ids_per_feature"][k])
            L += ["", f"**.NET example, exactly:** {d['scenarios_declared']} scenarios cover {len(d['ledger_features_with_scenario'])} distinct feature IDs because `{k}` is checked by {multi[k]} scenarios ({ids}). "
                  f"The fixture declares {d['fixture_counts']['features_declared']} feature IDs; the remaining one, `{', '.join(d['declared_in_fixture_not_in_ledger'])}`, is Windows-only and has no scenario, "
                  f"so it is neither in the ledger nor counted as verified. \"Nine scenarios\" and \"eight features\" are both correct and count different things; neither is global parity of the .NET application."]
    L += ["", "### Provenance of each candidate", "",
          "The stored candidate `author` field is `model` for a proposal from an external MCP client and also for the app's own AI route, so it cannot separate them. "
          "The labels below come from the recorded MCP call log, the demo client, or the candidate origin (basis stated). "
          "The app's own AI usage is none in all demos; for the dotnetapp and pecli demos an external client (itself an AI model, outside the app's routes and budget) wrote the Rust source.", "",
          "| Example | Candidate | Final | Authored by | Basis | Stored author field | App-routed AI calls |", "|---|---|---|---|---|---|---|"]
    for d in r["demos"]:
        for x in d["provenance"]:
            L.append(f"| {d['example']} | `{x['candidate_id']}` | {'yes' if x['final'] else 'no'} | {x['authored_by']} | {x['basis']} | {x['stored_author_field']} | {x['app_routed_ai_calls']} |")
    L += ["", "Recommended follow-up (not done here): record a `source` (`mcp` or `controller`) in candidate metadata so provenance does not depend on logs.",
          "", "## Windows (interactive desktop)", "", "PENDING. No interactive Windows desktop results are included in this report. Installer, WebView2, UI capture, Hermes desktop and DPAPI results will be added by the Windows run.",
          "", "## Windows CI", "", "PENDING. No Windows CI results are included in this report."]
    L += ["", "## Windows release gates (not certified here)", ""] + [f"- {g}" for g in r["windows_gates"]]
    L += ["", "## Hermes gates", ""] + [f"- {g}" for g in r["hermes_gates"]]
    L += ["", "## Limits and remaining user-dependent actions", ""] + [f"- {l}" for l in r["limits"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    r = build()
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "final-report.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    (ROOT / "reports" / "final-report.md").write_text(render(r), encoding="utf-8")
    print("wrote reports/final-report.md and .json")
