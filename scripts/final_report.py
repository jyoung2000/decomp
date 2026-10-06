#!/usr/bin/env python3
"""Generate reports/final-report.{md,json} from recorded test runs, live doctor output, fixture manifests and demo evidence.

Nothing here is invented: test counts come from reports/test-runs.json (appended by scripts/record_test_run.py from real pytest
output), backend states from `rebuildctl doctor --json` run now, demo outcomes from examples/*/evidence/parity-report.json.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = os.environ.get("REBUILD_PY", "/opt/rebuild-tools/venv/bin/python")


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
    raw = sh([PY, "-m", "rebuild_controller.cli.main", "doctor", "--json"], cwd=ROOT / "controller")
    try:
        return json.loads(raw[raw.find("{"):])
    except Exception:
        return {"error": raw[-2000:]}


def test_runs() -> list[dict]:
    p = ROOT / "reports" / "test-runs.json"
    return json.loads(p.read_text()) if p.exists() else []


def demos() -> list[dict]:
    out = []
    for d in sorted((ROOT / "examples").glob("*/evidence/parity-report.json")):
        rep = json.loads(d.read_text())
        out.append({"example": d.parents[1].name, "case": rep["case"]["name"], "target": rep["case"]["target_language"], "parity": rep["parity"],
                    "candidate": rep.get("candidate"), "host": rep.get("host"), "ai_usage": rep.get("ai_usage"), "comparisons": len(rep.get("comparisons", []))})
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
        "generated_at": datetime.now(timezone.utc).isoformat(), "host": {"os": sh(["uname", "-sr"]), "certifies_windows": os.name == "nt"},
        "commits": sh(["git", "log", "--oneline", "-30"]).splitlines(), "branch": sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "pins": pins(), "backends": doctor(), "test_runs": test_runs(), "fixtures": {k: {"kind": v.get("kind"), "oracle": v.get("oracle"), "features": len(v.get("features", []))} for k, v in fixtures().get("fixtures", {}).items()},
        "demos": demos(), "windows_gates": gates(), "hermes_gates": hermes_gates(),
        "limits": [
            "No interactive Windows host in this session: installer/WebView2/UI capture/Hermes desktop/DPAPI are handoffs (docs/WINDOWS_RELEASE_GATES.md).",
            "PE originals were executed under wine; the reports label that runner non-certifying.",
            "No provider API key was supplied: provider adapters are covered by mocked protocol tests only; zero paid calls were made.",
            "Unity IL2CPP, GameMaker, Android/JVM, Unreal profiles are detected but marked experimental/unverified (no backend run).",
            "Godot → Bevy produces a buildable scaffold plus recovered project; gameplay parity is untested because the original cannot run here and no scenarios were declared.",
        ],
    }


def render(r: dict) -> str:
    L = [f"# Rebuild Studio — final report", "", f"Generated {r['generated_at']} on {r['host']['os']} (certifies Windows: {r['host']['certifies_windows']}). Branch `{r['branch']}`.", "",
         "## Implementation commits", ""] + [f"- `{c}`" for c in r["commits"]]
    L += ["", "## Dependency pins", "", f"- Controller: {', '.join(r['pins']['controller_python_deps'])}", f"- UI: {json.dumps(r['pins']['ui_deps'])}", f"- Tauri crates: {json.dumps(r['pins']['tauri_crates'])}",
          f"- Host tools: {json.dumps(r['pins']['host_tools'])}", f"- rizin build manifest: {json.dumps(r['pins']['rizin_build_manifest'])[:600] if r['pins']['rizin_build_manifest'] else 'n/a'}"]
    L += ["", "## Backend adapters on this host (live doctor)", "", "| Backend | Availability | Tools |", "|---|---|---|"]
    for b in r["backends"].get("backends", []):
        L.append(f"| {b['backend_id']} | {b['availability']} | " + "; ".join(f"{t['name']} {t['availability']} {t.get('version') or ''}" for t in b.get("tools", [])) + " |")
    L += ["", "## Test runs (recorded from real executions)", "", "| Suite | Command | Passed | Failed | Skipped | When |", "|---|---|---|---|---|---|"]
    for t in r["test_runs"]:
        L.append(f"| {t['name']} | `{t['command']}` | {t['passed']} | {t['failed']} | {t['skipped']} | {t['when']} |")
    L += ["", "## Fixtures", ""] + [f"- **{k}**: {v['kind']}, {v['features']} declared features, oracle: {v['oracle']}" for k, v in r["fixtures"].items()]
    L += ["", "## Demonstrations (verifier-decided)", ""]
    for d in r["demos"]:
        p = d["parity"]
        author = (d.get("candidate") or {}).get("author") or "controller"
        L.append(f"- **{d['example']}** ({d['case']} → {d['target']}): full parity {'YES' if p['full_parity'] else 'NO'}; {p['verified']}/{p['features_total']} features verified, {p['failed']} failed, {p['untested']} untested; {d['comparisons']} comparisons; candidate author: {author}; AI calls via app routes: {d['ai_usage'] or 'none'}; host certifies Windows: {(d['host'] or {}).get('host_certifies_windows', 'n/a')}")
    L += ["", "## Windows release gates (not certified here)", ""] + [f"- {g}" for g in r["windows_gates"]]
    L += ["", "## Hermes gates", ""] + [f"- {g}" for g in r["hermes_gates"]]
    L += ["", "## Limits and remaining user-dependent actions", ""] + [f"- {l}" for l in r["limits"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    r = build()
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "final-report.json").write_text(json.dumps(r, indent=1, default=str))
    (ROOT / "reports" / "final-report.md").write_text(render(r))
    print("wrote reports/final-report.md and .json")
