"""Regenerate expected/scenarios.json for the tinycalc fixture by building and running the correct reference implementation.

usage: python make_oracle.py   (needs cargo). The oracle uses the fixture-oracle format read by rebuild_controller.fixture_oracle.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIOS = [
    ("sum_basic", "tinycalc.sum", "sum of three numbers", [["sum", "3", "4", "5"]]),
    ("max_basic", "tinycalc.max", "maximum of three numbers", [["max", "3", "9", "4"]]),
    ("err_not_a_number", "tinycalc.errors", "a non-numeric argument is rejected with exit code 2", [["sum", "1", "x"]]),
]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "ref"
        shutil.copytree(HERE / "reference", proj)
        subprocess.run(["cargo", "build", "--release", "--quiet"], cwd=proj, check=True)
        exe = proj / "target" / "release" / ("tinycalc.exe" if sys.platform == "win32" else "tinycalc")
        scenarios = []
        for sid, feat, desc, runs in SCENARIOS:
            steps = []
            for args in runs:
                p = subprocess.run([str(exe), *args], capture_output=True, text=True)
                steps.append({"args": args, "exit_code": p.returncode, "stdin": None, "stdout": p.stdout, "stderr": p.stderr,
                              "stdout_normalized": p.stdout.replace("\r\n", "\n"), "stderr_normalized": p.stderr.replace("\r\n", "\n")})
            scenarios.append({"id": sid, "feature": feat, "description": desc, "setup_files": {}, "final_files": {}, "steps": steps})
        doc = {"fixture": "tinycalc", "launcher": ["wine", "{ORIG}/tinycalc.exe"],
               "oracle": {"certifying": False, "runner": "native", "note": "outputs recorded from the correct reference implementation"},
               "observable_on": "windows_native", "newline_policy": "compare stdout/stderr CRLF-normalised", "scenarios": scenarios}
        out = HERE / "expected" / "scenarios.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out} ({len(scenarios)} scenarios)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
