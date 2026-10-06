"""Browser channels: DOM text, localStorage/hash, service worker/offline behaviour, screenshot — via the Node harness."""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..ids import sha256_bytes
from .base import ComparisonResult
from .images import compare_images

HARNESS = Path(__file__).parent / "web_harness.mjs"


def harness_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "harness"


def chromium_path() -> str | None:
    env = os.environ.get("REBUILD_CHROMIUM")
    if env and Path(env).exists():
        return env
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pat in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
        hits = sorted(glob.glob(str(Path(base) / pat)))
        if hits:
            return hits[-1]
    return None


def web_available() -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        return False, "node not installed"
    if not (harness_dir() / "node_modules" / "playwright").exists():
        return False, f"playwright not installed in {harness_dir()} (run npm ci there)"
    if not chromium_path():
        return False, "no Chromium found (set REBUILD_CHROMIUM or PLAYWRIGHT_BROWSERS_PATH)"
    return True, "ok"


def run_web_scenario(url: str, scenario: dict[str, Any], out_dir: Path, *, timeout: float = 120) -> dict[str, Any]:
    ok, why = web_available()
    if not ok:
        raise RuntimeError(why)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = {"url": url, "viewport": scenario.get("viewport", {"width": 1280, "height": 800}), "actions": scenario.get("actions", []),
            "capture": {"text_selectors": scenario.get("text_selectors", ["body"]), "storage": True, "sw": scenario.get("sw", True),
                        "screenshot": str(out_dir / "screenshot.png") if scenario.get("screenshot", True) else None},
            "executablePath": chromium_path(), "locale": scenario.get("locale", "en-US"), "timezone": scenario.get("timezone", "UTC"),
            "dpr": scenario.get("dpr", 1), "colorScheme": scenario.get("color_scheme", "light"), "wait_for": scenario.get("wait_for")}
    spec_path = out_dir / "spec.json"; spec_path.write_text(json.dumps(spec))
    rec_path = out_dir / "record.json"
    env = dict(os.environ); env["NODE_PATH"] = str(harness_dir() / "node_modules")
    p = subprocess.run(["node", str(HARNESS), str(spec_path), str(rec_path)], cwd=str(harness_dir()), capture_output=True, timeout=timeout, env=env)
    if p.returncode != 0 or not rec_path.exists():
        raise RuntimeError(f"web harness failed: {p.stderr.decode('utf-8', 'replace')[-2000:]}")
    rec = json.loads(rec_path.read_text())
    rec["command"] = f"node {HARNESS.name} spec.json record.json"
    return rec


def compare_web_capture(baseline: dict[str, Any], candidate: dict[str, Any], *, tolerance: dict[str, Any] | None = None,
                        artifacts_dir: Path | None = None) -> list[ComparisonResult]:
    tolerance = tolerance or {}
    out: list[ComparisonResult] = []
    bsteps, csteps = baseline.get("steps", []), candidate.get("steps", [])
    cmd = candidate.get("command", "")
    # DOM text per step
    dom_mism = []
    for i, (b, c) in enumerate(zip(bsteps, csteps)):
        bt, ct = b.get("text", {}), c.get("text", {})
        for sel in bt:
            if bt.get(sel) != ct.get(sel):
                dom_mism.append({"step": i, "label": b.get("label"), "selector": sel, "expected": (bt.get(sel) or "")[:500], "actual": (ct.get(sel) or "")[:500]})
    if len(csteps) != len(bsteps):
        dom_mism.append({"error": "step count differs", "expected": len(bsteps), "actual": len(csteps)})
    out.append(ComparisonResult("dom", "innerText:exact(per step)", "pass" if not dom_mism else "fail", {"mismatches": dom_mism[:50]},
                                original_hash=_h([s.get("text") for s in bsteps]), candidate_hash=_h([s.get("text") for s in csteps]), command=cmd))
    # storage + hash
    st_mism = []
    for i, (b, c) in enumerate(zip(bsteps, csteps)):
        if b.get("localStorage") != c.get("localStorage") or b.get("hash") != c.get("hash"):
            st_mism.append({"step": i, "expected": {"localStorage": b.get("localStorage"), "hash": b.get("hash")}, "actual": {"localStorage": c.get("localStorage"), "hash": c.get("hash")}})
    out.append(ComparisonResult("storage", "localStorage+hash:exact", "pass" if not st_mism else "fail", {"mismatches": st_mism[:20]},
                                original_hash=_h([(s.get("localStorage"), s.get("hash")) for s in bsteps]), candidate_hash=_h([(s.get("localStorage"), s.get("hash")) for s in csteps]), command=cmd))
    # offline / service worker
    off_mism = []
    for i, (b, c) in enumerate(zip(bsteps, csteps)):
        if "serviceWorker" in b:
            bsw, csw = b["serviceWorker"], c.get("serviceWorker") or {}
            if bsw.get("registered") != csw.get("registered") or bool(b.get("manifest")) != bool(c.get("manifest")):
                off_mism.append({"step": i, "expected": {"sw": bsw, "manifest": b.get("manifest")}, "actual": {"sw": csw, "manifest": c.get("manifest")}})
        if ("error" in b) != ("error" in c):
            off_mism.append({"step": i, "label": b.get("label"), "expected_error": b.get("error"), "actual_error": c.get("error")})
    out.append(ComparisonResult("offline", "sw_registered+manifest_present+reload_outcome:exact", "pass" if not off_mism else "fail", {"mismatches": off_mism[:20]}, command=cmd))
    # screenshot
    if baseline.get("screenshot") and candidate.get("screenshot"):
        tol = float(tolerance.get("screenshot_max_diff_fraction", 0.0))
        delta = int(tolerance.get("screenshot_channel_delta", 0))
        diff_out = (artifacts_dir / "diff.png") if artifacts_dir else None
        r = compare_images(Path(baseline["screenshot"]), Path(candidate["screenshot"]), tolerance=tol, per_channel_delta=delta, diff_out=diff_out)
        r.artifacts = [baseline["screenshot"], candidate["screenshot"]] + r.artifacts
        r.command = cmd
        out.append(r)
    # console errors
    be, ce = baseline.get("console_errors", []), candidate.get("console_errors", [])
    out.append(ComparisonResult("stderr", "console_errors:count<=baseline", "pass" if len(ce) <= len(be) else "fail", {"expected_count": len(be), "actual": ce[:10]}, command=cmd))
    return out


def _h(o: Any) -> str:
    return sha256_bytes(json.dumps(o, sort_keys=True, default=str).encode())
