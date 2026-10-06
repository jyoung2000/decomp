"""Backend verification records: a backend is 'verified' only after its fixture regression passed on this host with this version.

Records live in <data_dir>/backend-verification.json and are keyed by backend id + tool version; a version change invalidates them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..ids import now_iso

# backend → pytest selection that exercises it against a real fixture (must pass completely)
REGRESSIONS: dict[str, list[str]] = {
    "rizin": ["tests/test_rizin.py", "tests/test_fixture_oracle.py::test_pecli_oracle_rejects_wrong_remake_and_accepts_original"],
    "ilspy": ["tests/test_ilspy.py"],
    "gdre": ["tests/test_gdre.py"],
    "jsweb": ["tests/test_jsweb.py", "tests/test_archive.py"],
}


def record_path(data_dir: Path) -> Path:
    return Path(data_dir) / "backend-verification.json"


def load(data_dir: Path) -> dict[str, Any]:
    p = record_path(data_dir)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def run_regression(backend_id: str, data_dir: Path, versions: dict[str, str | None], *, timeout: int = 1800) -> dict[str, Any]:
    """Run the backend's fixture regression with pytest (real tools) and record pass/fail bound to tool versions."""
    sel = REGRESSIONS.get(backend_id)
    if not sel:
        return {"backend": backend_id, "ok": False, "error": "no regression defined"}
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ); env["REBUILD_STUDIO_DATA"] = str(Path(data_dir) / "verify-tmp")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *sel]
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout, env=env)
        out = p.stdout[-4000:] + p.stderr[-1000:]
        ok = p.returncode == 0 and " failed" not in p.stdout and "error" not in p.stdout.lower().split("\n")[-1]
    except subprocess.TimeoutExpired:
        out, ok = "timeout", False
    rec = {"backend": backend_id, "ok": ok, "versions": versions, "command": " ".join(cmd), "when": now_iso(), "host": os.uname().nodename if hasattr(os, "uname") else "windows",
           "output_tail": out[-1500:]}
    all_recs = load(data_dir)
    all_recs[backend_id] = rec
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    record_path(data_dir).write_text(json.dumps(all_recs, indent=1))
    return rec


def is_verified(backend_id: str, data_dir: Path, versions: dict[str, str | None]) -> dict[str, Any] | None:
    rec = load(data_dir).get(backend_id)
    if rec and rec.get("ok") and rec.get("versions") == versions:
        return rec
    return None
