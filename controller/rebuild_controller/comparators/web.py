"""Browser channels: DOM text, localStorage/hash, service worker/offline behaviour, screenshot — via the Node harness."""
from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..ids import sha256_bytes
from .base import ComparisonResult
from .images import compare_images

HARNESS = Path(__file__).parent / "web_harness.mjs"


def _tools_dir() -> Path:
    from ..config import get_settings
    return Path(get_settings().tools_dir)


def harness_dir() -> Path:
    """Source checkout: controller/harness (npm ci). Installed app (frozen): <tools>/harness, filled by the Tools page."""
    import sys
    dev = Path(__file__).resolve().parents[2] / "harness"
    if not getattr(sys, "frozen", False) and (dev / "package.json").exists():
        return dev
    d = _tools_dir() / "harness"
    d.mkdir(parents=True, exist_ok=True)
    return d


def node_path() -> str | None:
    exe = "node.exe" if os.name == "nt" else "node"
    tool = _tools_dir() / "node" / exe
    return shutil.which("node") or (str(tool) if tool.is_file() else None)


def playwright_module() -> Path | None:
    """The Playwright package the harness loads: a dev `playwright` install, or `playwright-core` installed by the Tools page."""
    for cand in (harness_dir() / "node_modules" / "playwright", _tools_dir() / "playwright-core"):
        if (cand / "package.json").exists():
            return cand
    return None


def chromium_path() -> str | None:
    env = os.environ.get("REBUILD_CHROMIUM")
    if env and Path(env).exists():
        return env
    bases = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")]
    if os.name == "nt":
        bases.append(str(_tools_dir() / "pw-browsers"))
    for base in bases:
        for pat in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-win64/chrome.exe", "chromium-*/chrome-win/chrome.exe",
                    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
            hits = sorted(glob.glob(str(Path(base) / pat)))
            if hits:
                return hits[-1]
    if os.name == "nt":
        # Microsoft Edge is Chromium-based and ships with Windows 11; Playwright drives it via executablePath.
        for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if root and (Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe").is_file():
                return str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return None


def web_available() -> tuple[bool, str]:
    if not node_path():
        return False, "Node.js is not installed (install 'Node.js' on the Tools page)"
    if not playwright_module():
        return False, "the browser test library is not installed (install 'Browser test library (Playwright)' on the Tools page)"
    if not chromium_path():
        return False, "no Chromium-based browser found (Microsoft Edge or a Playwright Chromium)"
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
    # The page under test (original or candidate web app) runs inside Chromium's own renderer sandbox. The node/Chromium
    # tree itself runs in a Job Object (tree kill, memory/process caps) with an allow-listed environment, but at MEDIUM
    # integrity: Chromium's sandbox needs a normal-integrity browser process. Recorded as a downgrade in rec["isolation"].
    from .. import sandbox
    node = node_path() or "node"
    policy = sandbox.IsolationPolicy.from_spec(scenario.get("isolation") or {"integrity": "medium", "reason": "Chromium's renderer sandbox needs a medium-integrity browser process"},
                                               wall_time_s=timeout, process_memory_bytes=4 * sandbox.GiB, job_memory_bytes=8 * sandbox.GiB,
                                               max_processes=128, ui_restrictions="interactive",
                                               env_passthrough=("PLAYWRIGHT_BROWSERS_PATH", "REBUILD_CHROMIUM"))
    # Short private home (TEMP/APPDATA/profile) outside the case tree: Chromium's profile paths under a deep case work dir
    # exceed Windows' 260-character limit ("sql::Database is not opened" / mkdtemp ENOENT), so keep the prefix short.
    import tempfile
    import uuid
    iso_root = Path(tempfile.gettempdir()) / f"rsw-{uuid.uuid4().hex[:8]}"
    for d in (out_dir, iso_root):
        sandbox.prepare_work_dir(d, policy)
    env = sandbox.build_env(iso_root, program_dirs=[str(Path(node).parent)], policy=policy, declared={"REBUILD_HARNESS_DIR": str(harness_dir()),
                                                                                                         **({"REBUILD_PLAYWRIGHT_MODULE": str(playwright_module())} if playwright_module() and playwright_module().name == "playwright-core" else {})})
    try:
        p = sandbox.run([node, str(HARNESS), str(spec_path), str(rec_path)], work=iso_root, cwd=harness_dir(), policy=policy, env=env)
    finally:
        shutil.rmtree(iso_root, ignore_errors=True)
    if p.returncode != 0 or not rec_path.exists():
        why = "timed out" if p.timed_out else p.stderr.decode('utf-8', 'replace')[-2000:]
        raise RuntimeError(f"web harness failed: {why}")
    rec = json.loads(rec_path.read_text())
    rec["command"] = f"node {HARNESS.name} spec.json record.json"
    rec["isolation"] = {**p.isolation, "limits_triggered": p.triggered}
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
