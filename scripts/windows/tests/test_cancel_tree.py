"""Gate W10 (docs/WINDOWS_RELEASE_GATES.md): cancelling a job ends the WHOLE process tree it started.

Uses the real controller job runner (JobStore / JobRunner / StageContext.run) and its `kill_tree`
(`taskkill /F /T /PID` on Windows). The stage launches a parent that starts a long-running grandchild, the job is
cancelled through `JobStore.cancel`, and the grandchild must be gone within 15 s.

  Windows:  powershell.exe -> ping.exe -n 300 (grandchild)          <- the certifying run
  POSIX:    sh -c "sleep 300 &"                                     <- self-check of this test's logic on the Linux dev host

Run (from the repo root, with the controller installed):
  python -m pytest scripts/windows/tests/test_cancel_tree.py -v -p no:cacheprovider
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from rebuild_controller.cases import CaseStore
from rebuild_controller.config import Limits, Settings, set_settings
from rebuild_controller.events import EventLog
from rebuild_controller.jobs import JobRunner, JobState, JobStore, StageRegistry
from rebuild_controller.store.db import Database

WINDOWS = os.name == "nt"


def _alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie awaiting reaping by init counts as dead
        return ") Z " not in Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True


def _wait_for(path: Path, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def world(tmp_path):
    s = Settings(data_dir=tmp_path / "data", limits=Limits(lease_timeout_seconds=2, worker_heartbeat_seconds=0, max_stage_seconds=120))
    s.ensure_dirs()
    set_settings(s)
    db = Database(s.db_path)
    events = EventLog(db)
    jobs = JobStore(db, events, lease_timeout=s.limits.lease_timeout_seconds)
    registry = StageRegistry()
    runner = JobRunner(jobs, events, registry, s.limits)
    cases = CaseStore(db, events, s)
    yield jobs, runner, registry, cases, events
    db.close()


def test_cancel_kills_grandchild(world, tmp_path):
    jobs, runner, registry, cases, events = world
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.txt").write_text("hello")
    cid = cases.create_case(name="t", source_root=str(src), output_root=str(tmp_path / "out"), target_language="rust", output_type="exe")["case_id"]
    pidfile = tmp_path / "grandchild.pid"
    if WINDOWS:
        script = (
            "$p = Start-Process -FilePath ping.exe -ArgumentList '-n','300','127.0.0.1' -WindowStyle Hidden -PassThru; "
            f"Set-Content -LiteralPath '{pidfile}' -Value $p.Id; Wait-Process -Id $p.Id"
        )
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        command = ["sh", "-c", f"sleep 300 & echo $! > '{pidfile}'; wait"]
    started = threading.Event()

    def long_stage(ctx):
        started.set()
        ctx.run(command, timeout=120)
        return {}

    registry.add("long", long_stage)
    job = jobs.create(cid, "long", "long-running tree", {})
    t = threading.Thread(target=runner.run_pending)
    t.start()
    try:
        assert started.wait(10), "stage did not start"
        assert _wait_for(pidfile, 60), "parent did not report the grandchild pid"
        grandchild = int(pidfile.read_text().strip().lstrip("﻿"))
        assert _alive(grandchild), "grandchild must be running before the cancel"
        t0 = time.time()
        jobs.cancel(job.job_id)
        t.join(30)
        assert not t.is_alive(), "runner did not return after the cancel"
        deadline = time.time() + 15
        while _alive(grandchild) and time.time() < deadline:
            time.sleep(0.2)
        assert not _alive(grandchild), "grandchild survived the cancel (process tree not killed)"
        print(f"grandchild {grandchild} gone {time.time() - t0:.1f}s after cancel")
    finally:
        if t.is_alive():
            t.join(1)
    assert jobs.get(job.job_id).state == JobState.CANCELLED
    kinds = [e["kind"] for e in events.events_since(0)]
    assert "job.cancel_requested" in kinds and "job.cancelled" in kinds
