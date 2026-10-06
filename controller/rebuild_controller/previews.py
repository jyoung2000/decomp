"""Versioned runnable previews tied to exact candidates; process/port cleanup on stop and exit."""
from __future__ import annotations

import http.server
import secrets
import socket
import threading
from functools import partial
from pathlib import Path
from typing import Any

from .candidates import CandidateStore
from .cases import CaseStore
from .events import EventLog
from .ids import new_id, now_iso
from .jobs.runner import kill_tree
from .store.db import Database, loads


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Service-Worker-Allowed", "/")
        super().end_headers()


class PreviewManager:
    def __init__(self, db: Database, events: EventLog, candidates: CandidateStore, cases: CaseStore):
        self.db = db
        self.events = events
        self.candidates = candidates
        self.cases = cases
        self._instances: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def publish(self, case_id: str, candidate_id: str, *, kind: str, title: str, launch: dict[str, Any], available: list[str],
                incomplete: list[str], requirements: list[str], steps: list[str], plan_revision: int) -> dict[str, Any]:
        assert kind in ("real", "mockup", "recording", "fixture")
        c = self.candidates.get(candidate_id)
        pid = new_id("prev")
        # earlier previews of this case for other candidates become stale (history), never silently reused
        self.db.execute("UPDATE previews SET stale=1 WHERE case_id=? AND candidate_id<>?", (case_id, candidate_id))
        self.db.insert("previews", {"preview_id": pid, "case_id": case_id, "candidate_id": candidate_id, "plan_revision": plan_revision, "kind": kind,
                                    "title": title, "available": available, "incomplete": incomplete, "requirements": requirements, "steps": steps,
                                    "launch": launch, "build_hash": c["build_hash"], "verification": c["verification"], "stale": 0, "created_at": now_iso()})
        p = self.get(pid)
        self.events.emit("preview.published", {"preview": p}, case_id=case_id)
        return p

    def get(self, preview_id: str) -> dict[str, Any]:
        r = self.db.query_one("SELECT * FROM previews WHERE preview_id=?", (preview_id,))
        if not r:
            raise KeyError(preview_id)
        for k in ("available", "incomplete", "requirements", "steps"):
            r[k] = loads(r[k], [])
        r["launch"] = loads(r["launch"], {})
        r["stale"] = bool(r["stale"])
        c = self.candidates.get(r["candidate_id"])
        r["verification"] = c["verification"]
        r["running"] = [i["instance_id"] for i in self._instances.values() if i["preview_id"] == preview_id]
        return r

    def list(self, case_id: str) -> list[dict[str, Any]]:
        return [self.get(r["preview_id"]) for r in self.db.query("SELECT preview_id FROM previews WHERE case_id=? ORDER BY created_at DESC", (case_id,))]

    # -- launching ------------------------------------------------------
    def open(self, preview_id: str) -> dict[str, Any]:
        p = self.get(preview_id)
        if p["kind"] != "real":
            return {"kind": p["kind"], "opened": False, "message": f"{p['kind']} preview: not a runnable build", "preview": p}
        launch = p["launch"]
        iid = new_id("inst")
        if launch.get("type") == "browser":
            root = Path(launch["root"])
            if not root.is_dir():
                return {"opened": False, "message": "preview files missing (candidate dist not present)", "next_action": "rebuild the candidate"}
            port = _free_port()
            token = secrets.token_urlsafe(8)
            handler = partial(_QuietHandler, directory=str(root))
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
            url = f"http://127.0.0.1:{port}/{launch.get('entry', 'index.html')}#preview={token}"
            with self._lock:
                self._instances[iid] = {"instance_id": iid, "preview_id": preview_id, "kind": "browser", "server": srv, "port": port, "url": url}
            self.events.emit("preview.opened", {"preview_id": preview_id, "instance_id": iid, "url": url}, case_id=p["case_id"])
            return {"kind": "browser", "url": url, "instance_id": iid, "opened": True, "isolation": "separate origin per port; no shared state with original"}
        if launch.get("type") == "native":
            from . import sandbox
            cmd = list(launch["command"])
            cwd = launch.get("cwd")
            state_dir = Path(launch.get("state_dir") or (Path(cwd or ".") / ".preview-state"))
            try:
                policy = sandbox.IsolationPolicy.from_spec(launch.get("isolation"), wall_time_s=None, ui_restrictions="interactive",
                                                           process_memory_bytes=2 * sandbox.GiB, job_memory_bytes=4 * sandbox.GiB, max_processes=64)
                sandbox.prepare_work_dir(state_dir, policy)
                prog_dirs = [str(Path(cmd[0]).parent)] if Path(cmd[0]).is_absolute() else []
                env = sandbox.build_env(state_dir, program_dirs=prog_dirs, policy=policy,
                                        declared={"REBUILD_PREVIEW_STATE": str(state_dir), **launch.get("env", {})})
                proc = sandbox.spawn(cmd, work=state_dir, cwd=Path(cwd) if cwd else state_dir, policy=policy, env=env)
            except (OSError, ValueError, sandbox.SandboxError) as e:
                return {"opened": False, "message": f"could not launch: {e}", "next_action": "check launch requirements"}
            with self._lock:
                self._instances[iid] = {"instance_id": iid, "preview_id": preview_id, "kind": "native", "proc": proc}
            iso = getattr(proc, "isolation", None)
            self.events.emit("preview.opened", {"preview_id": preview_id, "instance_id": iid, "pid": proc.pid, "isolation": iso}, case_id=p["case_id"])
            return {"kind": "native", "instance_id": iid, "pid": proc.pid, "opened": True, "command": cmd, "isolated_state": str(state_dir),
                    "isolation": iso}
        return {"opened": False, "message": f"unsupported launch type {launch.get('type')}", "next_action": "build a runnable candidate first"}

    def stop(self, instance_id: str) -> bool:
        with self._lock:
            inst = self._instances.pop(instance_id, None)
        if not inst:
            return False
        if inst["kind"] == "browser":
            inst["server"].shutdown(); inst["server"].server_close()
        elif hasattr(inst["proc"], "kill_tree"):
            inst["proc"].kill_tree()     # sandboxed: whole job / process group
        else:
            kill_tree(inst["proc"])
        p = self.get(inst["preview_id"])
        self.events.emit("preview.stopped", {"preview_id": inst["preview_id"], "instance_id": instance_id}, case_id=p["case_id"])
        return True

    def stop_all(self) -> None:
        for iid in list(self._instances):
            self.stop(iid)

    def running(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in i.items() if k not in ("server", "proc")} for i in self._instances.values()]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
