from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..jobs.runner import StageContext, StageError

IGNORE = {"node_modules", ".git", "harness", "test", "tests"}


def build_web(ctx: StageContext, source_dir: Path, dist_dir: Path) -> dict[str, Any]:
    site = source_dir / "site" if (source_dir / "site").is_dir() else source_dir
    index = site / "index.html"
    if not index.exists():
        raise StageError("web candidate has no index.html")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    shutil.copytree(site, dist_dir, ignore=shutil.ignore_patterns(*IGNORE, "*.map.orig"))
    html = index.read_text("utf-8", "replace")
    manifest_href = _attr(html, r'<link[^>]+rel=["\']manifest["\'][^>]*href=["\']([^"\']+)')
    pwa: dict[str, Any] = {"manifest": None, "service_worker": None, "installable_static_checks": False, "issues": []}
    if manifest_href and (dist_dir / manifest_href).exists():
        try:
            m = json.loads((dist_dir / manifest_href).read_text("utf-8"))
            pwa["manifest"] = {"name": m.get("name"), "start_url": m.get("start_url"), "display": m.get("display"), "icons": len(m.get("icons", []))}
            if not m.get("name") or not m.get("start_url") or m.get("display") not in ("standalone", "fullscreen", "minimal-ui"):
                pwa["issues"].append("manifest lacks name/start_url/standalone display")
            if not any(i.get("sizes", "").startswith(("192", "512")) for i in m.get("icons", [])):
                pwa["issues"].append("manifest lacks 192/512 icons")
        except ValueError:
            pwa["issues"].append("manifest is not valid JSON")
    else:
        pwa["issues"].append("no manifest link")
    sw = re.search(r"serviceWorker\.register\(\s*['\"]([^'\"]+)", html + "\n" + "\n".join(p.read_text("utf-8", "replace") for p in dist_dir.glob("*.js")))
    if sw and (dist_dir / sw.group(1).lstrip("./")).exists():
        pwa["service_worker"] = sw.group(1)
    else:
        pwa["issues"].append("no service worker registration found")
    pwa["installable_static_checks"] = not pwa["issues"]
    return {"launch": {"type": "web", "root": ".", "entry": "index.html"}, "pwa": pwa, "files": sum(1 for p in dist_dir.rglob("*") if p.is_file()),
            "note": "static checks only; installability/offline/cache-upgrade are verified by the browser comparator"}


def _attr(html: str, pattern: str) -> str | None:
    m = re.search(pattern, html, re.I)
    return m.group(1) if m else None
