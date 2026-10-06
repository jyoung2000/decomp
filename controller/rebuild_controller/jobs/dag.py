"""Durable job DAG: creation, dependency gating, leases, attempts, cancellation, resume.

State machine (atomic transitions in SQLite):
  queued -> running -> completed | failed | cancelled | blocked
  failed -> queued (retry while attempt < max_attempts)
  blocked -> queued (when blocker cleared / dependency completes)
  any terminal -> needs_retest (when inputs/evidence change)
A job is runnable when all dependencies are completed and it holds no active lease.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..events import EventLog
from ..ids import new_id, now_iso, now_ts, stable_json_hash
from ..store.db import Database, loads


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NEEDS_RETEST = "needs_retest"


TERMINAL = {JobState.FAILED, JobState.CANCELLED, JobState.COMPLETED}


@dataclass
class Job:
    job_id: str
    case_id: str
    stage: str
    title: str
    inputs: dict[str, Any]
    input_hash: str
    state: JobState
    priority: int
    attempt: int
    max_attempts: int
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    blocker: str | None
    cancel_requested: bool
    milestone_id: str | None
    lease_owner: str | None = None
    lease_expires: float | None = None
    heartbeat_at: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Job":
        return cls(
            job_id=r["job_id"], case_id=r["case_id"], stage=r["stage"], title=r["title"],
            inputs=loads(r["inputs"], {}), input_hash=r["input_hash"], state=JobState(r["state"]),
            priority=r["priority"], attempt=r["attempt"], max_attempts=r["max_attempts"],
            progress=loads(r["progress"], {}), result=loads(r["result"]), error=r["error"], blocker=r["blocker"],
            cancel_requested=bool(r["cancel_requested"]), milestone_id=r["milestone_id"],
            lease_owner=r["lease_owner"], lease_expires=r["lease_expires"], heartbeat_at=r["heartbeat_at"],
            started_at=r["started_at"], finished_at=r["finished_at"], created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["state"] = self.state.value
        return d


class JobStore:
    def __init__(self, db: Database, events: EventLog, *, max_queue: int = 10_000, lease_timeout: float = 30.0):
        self.db = db
        self.events = events
        self.max_queue = max_queue
        self.lease_timeout = lease_timeout

    # -- creation -------------------------------------------------------
    def create(self, case_id: str, stage: str, title: str, inputs: dict[str, Any], *, depends_on: list[str] | None = None,
               priority: int = 100, max_attempts: int = 3, milestone_id: str | None = None) -> Job:
        depends_on = depends_on or []
        with self.db.transaction():
            n = self.db.query_one("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running','blocked')")
            if n and n["n"] >= self.max_queue:
                raise RuntimeError(f"job queue is full ({self.max_queue}); refusing new job")
            for d in depends_on:
                if not self.db.query_one("SELECT 1 FROM jobs WHERE job_id=?", (d,)):
                    raise ValueError(f"unknown dependency job {d}")
            job_id = new_id("job")
            ts = now_iso()
            ihash = stable_json_hash({"stage": stage, "inputs": inputs})
            state = JobState.QUEUED if self._deps_satisfied(depends_on) else JobState.BLOCKED
            self.db.insert("jobs", {
                "job_id": job_id, "case_id": case_id, "stage": stage, "title": title, "inputs": inputs,
                "input_hash": ihash, "state": state.value, "priority": priority, "attempt": 0, "max_attempts": max_attempts,
                "created_at": ts, "updated_at": ts, "progress": {}, "milestone_id": milestone_id,
                "blocker": None if state == JobState.QUEUED else "waiting on dependencies",
            })
            for d in depends_on:
                self.db.insert("job_deps", {"job_id": job_id, "depends_on": d})
            job = self.get(job_id)
        self.events.emit("job.created", {"job": job.to_dict(), "depends_on": depends_on}, case_id=case_id, job_id=job_id)
        return job

    def _deps_satisfied(self, deps: list[str]) -> bool:
        for d in deps:
            r = self.db.query_one("SELECT state FROM jobs WHERE job_id=?", (d,))
            if not r or r["state"] != JobState.COMPLETED.value:
                return False
        return True

    def add_dependency(self, job_id: str, depends_on: str) -> None:
        """Add an edge after creation (fan-out). A queued job whose new dependency is unfinished becomes blocked."""
        with self.db.transaction():
            if not self.db.query_one("SELECT 1 FROM jobs WHERE job_id=?", (depends_on,)):
                raise ValueError(f"unknown dependency job {depends_on}")
            self.db.execute("INSERT OR IGNORE INTO job_deps(job_id, depends_on) VALUES (?,?)", (job_id, depends_on))
            j = self.get(job_id)
            if j.state == JobState.QUEUED and not self._deps_satisfied(self.dependencies(job_id)):
                self.db.update("jobs", "job_id", job_id, {"state": JobState.BLOCKED.value, "blocker": "waiting on dependencies", "updated_at": now_iso()})

    def dependencies(self, job_id: str) -> list[str]:
        return [r["depends_on"] for r in self.db.query("SELECT depends_on FROM job_deps WHERE job_id=?", (job_id,))]

    def dependents(self, job_id: str) -> list[str]:
        return [r["job_id"] for r in self.db.query("SELECT job_id FROM job_deps WHERE depends_on=?", (job_id,))]

    # -- queries --------------------------------------------------------
    def get(self, job_id: str) -> Job:
        r = self.db.query_one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not r:
            raise KeyError(job_id)
        return Job.from_row(r)

    def list(self, case_id: str | None = None, states: list[JobState] | None = None) -> list[Job]:
        sql, params = "SELECT * FROM jobs", []
        clauses = []
        if case_id:
            clauses.append("case_id=?"); params.append(case_id)
        if states:
            clauses.append("state IN (%s)" % ",".join("?" * len(states))); params += [s.value for s in states]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority, created_at"
        return [Job.from_row(r) for r in self.db.query(sql, tuple(params))]

    def counts(self, case_id: str) -> dict[str, int]:
        rows = self.db.query("SELECT state, COUNT(*) AS n FROM jobs WHERE case_id=? GROUP BY state", (case_id,))
        out = {s.value: 0 for s in JobState}
        for r in rows:
            out[r["state"]] = r["n"]
        return out

    # -- leasing --------------------------------------------------------
    def claim_next(self, worker: str, stages: list[str] | None = None) -> Job | None:
        """Atomically claim the highest-priority runnable job."""
        now = now_ts()
        with self.db.transaction():
            sql = "SELECT * FROM jobs WHERE state='queued' AND cancel_requested=0"
            params: list[Any] = []
            if stages:
                sql += " AND stage IN (%s)" % ",".join("?" * len(stages)); params += stages
            sql += " ORDER BY priority, created_at"
            for r in self.db.query(sql, tuple(params)):
                if not self._deps_satisfied(self.dependencies(r["job_id"])):
                    continue
                ts = now_iso()
                attempt = r["attempt"] + 1
                aid = new_id("att")
                self.db.update("jobs", "job_id", r["job_id"], {
                    "state": JobState.RUNNING.value, "lease_owner": worker, "lease_expires": now + self.lease_timeout,
                    "heartbeat_at": now, "started_at": ts, "updated_at": ts, "attempt": attempt, "error": None, "blocker": None,
                })
                self.db.insert("job_attempts", {"attempt_id": aid, "job_id": r["job_id"], "attempt": attempt,
                                                "started_at": ts, "worker": worker})
                job = self.get(r["job_id"])
                self.events.emit("job.started", {"job_id": job.job_id, "attempt": attempt, "worker": worker, "stage": job.stage,
                                                 "title": job.title}, case_id=job.case_id, job_id=job.job_id)
                return job
        return None

    def heartbeat(self, job_id: str, worker: str, progress: dict[str, Any] | None = None) -> bool:
        """Extend lease. Returns False if the job was cancelled or lease lost (worker must stop)."""
        now = now_ts()
        with self.db.transaction():
            r = self.db.query_one("SELECT state, lease_owner, cancel_requested FROM jobs WHERE job_id=?", (job_id,))
            if not r or r["lease_owner"] != worker or r["state"] != JobState.RUNNING.value:
                return False
            fields: dict[str, Any] = {"heartbeat_at": now, "lease_expires": now + self.lease_timeout}
            if progress is not None:
                fields["progress"] = progress
            self.db.update("jobs", "job_id", job_id, fields)
            cancelled = bool(r["cancel_requested"])
        if progress is not None:
            job = self.get(job_id)
            self.events.emit("job.progress", {"job_id": job_id, "progress": progress, "stage": job.stage}, case_id=job.case_id, job_id=job_id)
        return not cancelled

    # -- completion -----------------------------------------------------
    def complete(self, job_id: str, worker: str, result: dict[str, Any]) -> None:
        self._finish(job_id, worker, JobState.COMPLETED, result=result)

    def fail(self, job_id: str, worker: str, error: str, *, retry: bool = True, blocker: str | None = None) -> JobState:
        with self.db.transaction():
            r = self.db.query_one("SELECT attempt, max_attempts, cancel_requested FROM jobs WHERE job_id=?", (job_id,))
            if r is None:
                raise KeyError(job_id)
            if r["cancel_requested"]:
                self._finish(job_id, worker, JobState.CANCELLED, error=error)
                return JobState.CANCELLED
            if blocker is not None:
                self._finish(job_id, worker, JobState.BLOCKED, error=error, blocker=blocker)
                return JobState.BLOCKED
            if retry and r["attempt"] < r["max_attempts"]:
                ts = now_iso()
                self.db.update("jobs", "job_id", job_id, {"state": JobState.QUEUED.value, "lease_owner": None, "lease_expires": None,
                                                         "error": error, "updated_at": ts})
                self._close_attempt(job_id, "retry", error)
                job = self.get(job_id)
                self.events.emit("job.retry", {"job_id": job_id, "attempt": r["attempt"], "error": error[:2000]}, case_id=job.case_id, job_id=job_id)
                return JobState.QUEUED
            self._finish(job_id, worker, JobState.FAILED, error=error)
            return JobState.FAILED

    def _close_attempt(self, job_id: str, outcome: str, error: str | None) -> None:
        self.db.execute("UPDATE job_attempts SET finished_at=?, outcome=?, error=? WHERE job_id=? AND finished_at IS NULL",
                        (now_iso(), outcome, error, job_id))

    def _finish(self, job_id: str, worker: str | None, state: JobState, *, result: dict | None = None, error: str | None = None,
                blocker: str | None = None) -> None:
        with self.db.transaction():
            ts = now_iso()
            self.db.update("jobs", "job_id", job_id, {"state": state.value, "lease_owner": None, "lease_expires": None,
                                                     "finished_at": ts, "updated_at": ts, "result": result, "error": error, "blocker": blocker})
            self._close_attempt(job_id, state.value, error)
            job = self.get(job_id)
            if state == JobState.COMPLETED:
                for dep in self.dependents(job_id):
                    d = self.get(dep)
                    if d.state == JobState.BLOCKED and self._deps_satisfied(self.dependencies(dep)):
                        self.db.update("jobs", "job_id", dep, {"state": JobState.QUEUED.value, "blocker": None, "updated_at": ts})
                        self.events.emit("job.unblocked", {"job_id": dep}, case_id=d.case_id, job_id=dep)
            elif state in (JobState.FAILED, JobState.CANCELLED, JobState.BLOCKED):
                reason = f"dependency {job_id} {state.value}" + (f": {blocker}" if blocker else "")
                for dep in self._all_dependents(job_id):
                    d = self.get(dep)
                    if d.state in (JobState.BLOCKED, JobState.QUEUED):
                        self.db.update("jobs", "job_id", dep, {"state": JobState.BLOCKED.value, "updated_at": ts, "blocker": reason})
                        self.events.emit("job.blocked", {"job_id": dep, "blocker": reason}, case_id=d.case_id, job_id=dep)
        payload = {"job_id": job_id, "state": state.value, "stage": job.stage, "title": job.title, "error": (error or "")[:4000], "blocker": blocker}
        if result is not None:
            payload["result_keys"] = sorted(result.keys())
        self.events.emit(f"job.{state.value}", payload, case_id=job.case_id, job_id=job_id)

    def _all_dependents(self, job_id: str) -> list[str]:
        seen, stack, out = set(), [job_id], []
        while stack:
            j = stack.pop()
            for d in self.dependents(j):
                if d not in seen:
                    seen.add(d); out.append(d); stack.append(d)
        return out

    # -- control --------------------------------------------------------
    def cancel(self, job_id: str, *, cascade: bool = True) -> list[str]:
        """Request cancellation. Running jobs are flagged (runner kills process tree); queued/blocked jobs cancel immediately."""
        cancelled: list[str] = []
        targets = [job_id] + (self._all_dependents(job_id) if cascade else [])
        with self.db.transaction():
            for jid in targets:
                j = self.get(jid)
                if j.state in TERMINAL:
                    continue
                if j.state == JobState.RUNNING:
                    self.db.update("jobs", "job_id", jid, {"cancel_requested": 1, "updated_at": now_iso()})
                    self.events.emit("job.cancel_requested", {"job_id": jid}, case_id=j.case_id, job_id=jid)
                else:
                    self._finish(jid, None, JobState.CANCELLED, error="cancelled by user")
                cancelled.append(jid)
        return cancelled

    def cancel_case(self, case_id: str) -> list[str]:
        out = []
        for j in self.list(case_id, [JobState.QUEUED, JobState.BLOCKED, JobState.RUNNING]):
            out += self.cancel(j.job_id, cascade=False)
        return out

    def resume(self, job_id: str) -> Job:
        """Re-queue a failed/cancelled/needs_retest/blocked(no-dep) job preserving its attempt history."""
        with self.db.transaction():
            j = self.get(job_id)
            if j.state == JobState.RUNNING:
                return j
            blocked_by_deps = not self._deps_satisfied(self.dependencies(job_id))
            state = JobState.BLOCKED if blocked_by_deps else JobState.QUEUED
            self.db.update("jobs", "job_id", job_id, {"state": state.value, "cancel_requested": 0, "attempt": 0 if j.state != JobState.BLOCKED else j.attempt,
                                                     "error": None, "blocker": "waiting on dependencies" if blocked_by_deps else None,
                                                     "updated_at": now_iso(), "finished_at": None})
            j = self.get(job_id)
        self.events.emit("job.resumed", {"job_id": job_id, "state": j.state.value}, case_id=j.case_id, job_id=job_id)
        return j

    def mark_needs_retest(self, job_id: str, reason: str) -> None:
        with self.db.transaction():
            j = self.get(job_id)
            if j.state == JobState.COMPLETED:
                self.db.update("jobs", "job_id", job_id, {"state": JobState.NEEDS_RETEST.value, "blocker": reason, "updated_at": now_iso()})
                self.events.emit("job.needs_retest", {"job_id": job_id, "reason": reason}, case_id=j.case_id, job_id=job_id)
            for d in self.dependents(job_id):
                self.mark_needs_retest(d, f"upstream {job_id}: {reason}")

    def recover_stale(self, *, now: float | None = None) -> list[str]:
        """On startup or periodically: jobs whose lease expired are re-queued (or failed if attempts exhausted)."""
        now = now if now is not None else now_ts()
        recovered = []
        with self.db.transaction():
            for r in self.db.query("SELECT job_id, attempt, max_attempts, case_id, lease_owner FROM jobs WHERE state='running' AND (lease_expires IS NULL OR lease_expires < ?)", (now,)):
                jid = r["job_id"]
                self.events.emit("job.lease_expired", {"job_id": jid, "worker": r["lease_owner"]}, case_id=r["case_id"], job_id=jid)
                self.fail(jid, r["lease_owner"] or "", "worker lease expired (crash or stall)", retry=True)
                recovered.append(jid)
        return recovered

    def stalled(self, *, timeout: float | None = None) -> list[Job]:
        """Running jobs without a heartbeat within timeout: UI shows these as stale/unknown."""
        timeout = timeout or self.lease_timeout
        cutoff = now_ts() - timeout
        return [Job.from_row(r) for r in self.db.query("SELECT * FROM jobs WHERE state='running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (cutoff,))]
