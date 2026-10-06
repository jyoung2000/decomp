#!/usr/bin/env python3
"""Append a real pytest/vitest/cargo result to reports/test-runs.json. Usage: record_test_run.py <name> <command> <logfile>"""
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path
name, command, log = sys.argv[1], sys.argv[2], Path(sys.argv[3]).read_text(errors="replace")
m = re.search(r"(\d+) passed", log); f = re.search(r"(\d+) failed", log); s = re.search(r"(\d+) skipped", log)
if not m and "Tests" in log:  # vitest
    m = re.search(r"Tests\s+(\d+) passed", log); f = re.search(r"(\d+) failed", log)
p = Path(__file__).resolve().parents[1] / "reports" / "test-runs.json"
runs = json.loads(p.read_text()) if p.exists() else []
runs = [r for r in runs if r["name"] != name]
runs.append({"name": name, "command": command, "passed": int(m.group(1)) if m else 0, "failed": int(f.group(1)) if f else 0, "skipped": int(s.group(1)) if s else 0,
             "when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "log_tail": log[-400:]})
p.parent.mkdir(exist_ok=True); p.write_text(json.dumps(runs, indent=1))
print(runs[-1]["name"], runs[-1]["passed"], runs[-1]["failed"], runs[-1]["skipped"])
