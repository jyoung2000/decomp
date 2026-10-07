"""Job runner: executes registered stage functions under leases with bounded subprocesses.

- One runner per process, N worker threads (bounded by Limits.max_concurrent_jobs).
- Stage functions receive a StageContext with heartbeat/progress/subprocess helpers.
- Cancellation kills the owned process tree (process group on POSIX, taskkill /T on Windows).
- Timeouts are visible failures (job.failed with error 'timeout'), never silent spinners.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Limits
from ..events import EventLog
from ..ids import new_id, now_ts
from .dag import Job, JobState, JobStore


class StageError(Exception):
    """Raised by stage code for a visible, non-retryable failure (or retryable if retry=True)."""

    def __init__(self, message: str, *, retry: bool = False, blocker: str | None = None):
        super().__init__(message)
        self.retry = retry
        self.blocker = blocker


class Cancelled(Exception):
    pass


@dataclass
class SubprocessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool
    duration_s: float
    command: list[str]

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")


@dataclass
class StageContext:
    job: Job
    jobs: JobStore
    events: EventLog
    worker: str
    limits: Limits
    services: dict[str, Any] = field(default_factory=dict)
    _procs: list[subprocess.Popen] = field(default_factory=list)
    _cancelled: bool = False
    _last_hb: float = 0.0
    _limiter: Any = None

    # -- liveness -------------------------------------------------------
    def heartbeat(self, progress: dict[str, Any] | None = None, *, force: bool = False) -> None:
        now = now_ts()
        if not force and progress is None and now - self._last_hb < self.limits.worker_heartbeat_seconds:
            return
        self._last_hb = now
        alive = self.jobs.heartbeat(self.job.job_id, self.worker, progress)
        if not alive:
            self._cancelled = True
            self.kill_all()
            raise Cancelled(f"job {self.job.job_id} cancelled")

    def progress(self, **fields: Any) -> None:
        """Report measured progress. Callers must supply real counts; never synthesize percentages here."""
        self.heartbeat(fields, force=True)

    def log(self, text: str, level: str = "info", detail: str | None = None, *, plan_item_id: str | None = None,
            key: str | None = None, **extra: Any) -> None:
        """One plain-English line for the live log (persisted ``job.log`` event). Redacted, bounded and rate limited (<= 5 info
        lines/s per job); pass ``key`` so repeated progress lines coalesce to the latest. Never pass prompts or tool credentials."""
        from .. import livelog
        if self._limiter is None:
            self._limiter = livelog.LogLimiter()
        livelog.emit_job_log(self.events, self.job, self._limiter, text, level, detail, plan_item_id=plan_item_id, key=key, extra=extra)

    def log_failure(self, what: str, stderr: str | bytes | None, *, detail_lines: int = 5) -> None:
        """Error line plus the last few (redacted, bounded) lines of the tool's stderr."""
        from .. import livelog
        self.log(what, "error", livelog.tail_lines(stderr, detail_lines) or None)

    def flush_log(self) -> None:
        if self._limiter is not None:
            from .. import livelog
            livelog.flush_job_log(self.events, self.job, self._limiter)

    # -- subprocesses ---------------------------------------------------
    def run(self, command: list[str], *, cwd: str | os.PathLike | None = None, env: dict[str, str] | None = None,
            timeout: float | None = None, stdin: bytes | None = None, check: bool = False) -> SubprocessResult:
        """Run a bounded subprocess tied to this job. Output capped; process tree killed on cancel/timeout."""
        timeout = timeout or self.limits.max_stage_seconds
        cap = self.limits.max_subprocess_output_bytes
        kwargs: dict[str, Any] = {"cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                  "stdin": subprocess.PIPE if stdin is not None else subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        start = now_ts()
        proc = subprocess.Popen(command, **kwargs)
        self._procs.append(proc)
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        sizes = [0, 0]
        truncated = [False]

        def pump(stream, chunks, idx):
            try:
                while True:
                    b = stream.read(65536)
                    if not b:
                        break
                    if sizes[idx] + len(b) > cap:
                        b = b[: max(0, cap - sizes[idx])]
                        truncated[0] = True
                    if b:
                        chunks.append(b); sizes[idx] += len(b)
                    if truncated[0]:
                        # keep draining to avoid blocking the child, but drop data
                        while stream.read(65536):
                            pass
                        break
            except Exception:
                pass

        t1 = threading.Thread(target=pump, args=(proc.stdout, out_chunks, 0), daemon=True)
        t2 = threading.Thread(target=pump, args=(proc.stderr, err_chunks, 1), daemon=True)
        t1.start(); t2.start()
        if stdin is not None:
            try:
                proc.stdin.write(stdin); proc.stdin.close()
            except Exception:
                pass
        timed_out = False
        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                try:
                    self.heartbeat()
                except Cancelled:
                    kill_tree(proc)
                    raise
                if now_ts() - start > timeout:
                    timed_out = True
                    kill_tree(proc)
                    proc.wait(timeout=10)
                    break
        t1.join(timeout=5); t2.join(timeout=5)
        self._procs.remove(proc)
        res = SubprocessResult(proc.returncode, b"".join(out_chunks), b"".join(err_chunks), truncated[0], timed_out,
                               now_ts() - start, list(command))
        if timed_out:
            raise StageError(f"timeout after {timeout:.0f}s running {command[0]}", retry=False)
        if check and res.returncode != 0:
            raise StageError(f"{command[0]} exited {res.returncode}: {res.stderr.decode('utf-8', 'replace')[-2000:]}")
        return res

    def kill_all(self) -> None:
        for p in list(self._procs):
            kill_tree(p)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, subprocess.SubprocessError, OSError):
        pass


StageFn = Callable[[StageContext], dict[str, Any]]


class StageRegistry:
    def __init__(self):
        self._stages: dict[str, StageFn] = {}

    def register(self, name: str) -> Callable[[StageFn], StageFn]:
        def deco(fn: StageFn) -> StageFn:
            self._stages[name] = fn
            return fn
        return deco

    def add(self, name: str, fn: StageFn) -> None:
        self._stages[name] = fn

    def get(self, name: str) -> StageFn | None:
        return self._stages.get(name)

    def names(self) -> list[str]:
        return sorted(self._stages)


class JobRunner:
    def __init__(self, jobs: JobStore, events: EventLog, registry: StageRegistry, limits: Limits,
                 services: dict[str, Any] | None = None, worker_name: str | None = None):
        self.jobs = jobs
        self.events = events
        self.registry = registry
        self.limits = limits
        self.services = services or {}
        self.worker_name = worker_name or f"worker-{os.getpid()}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active: dict[str, StageContext] = {}
        self._lock = threading.Lock()

    def start(self, n: int | None = None) -> None:
        self.jobs.recover_stale()
        n = n or self.limits.max_concurrent_jobs
        for i in range(n):
            t = threading.Thread(target=self._loop, args=(f"{self.worker_name}/{i}",), daemon=True, name=f"job-worker-{i}")
            t.start(); self._threads.append(t)
        self._threads.append(threading.Thread(target=self._watchdog, daemon=True, name="job-watchdog"))
        self._threads[-1].start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        with self._lock:
            for ctx in self._active.values():
                ctx.kill_all()
        for t in self._threads:
            t.join(timeout=timeout)

    def run_pending(self, max_jobs: int = 10_000) -> int:
        """Synchronously drain runnable jobs in this thread (used by CLI/tests)."""
        n = 0
        while n < max_jobs:
            job = self.jobs.claim_next(self.worker_name)
            if job is None:
                break
            self._execute(job, self.worker_name)
            n += 1
        return n

    def _loop(self, worker: str) -> None:
        while not self._stop.is_set():
            job = self.jobs.claim_next(worker)
            if job is None:
                self._stop.wait(0.25)
                continue
            self._execute(job, worker)

    def _watchdog(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.limits.lease_timeout_seconds)
            try:
                self.jobs.recover_stale()
            except Exception:
                pass
            self.events.emit("controller.heartbeat", {"worker": self.worker_name, "active": len(self._active)})

    def _execute(self, job: Job, worker: str) -> None:
        fn = self.registry.get(job.stage)
        ctx = StageContext(job=job, jobs=self.jobs, events=self.events, worker=worker, limits=self.limits, services=self.services)
        with self._lock:
            self._active[job.job_id] = ctx
        try:
            if fn is None:
                self.jobs.fail(job.job_id, worker, f"no stage implementation registered for '{job.stage}'", retry=False,
                               blocker=f"stage '{job.stage}' unavailable")
                return
            ctx.heartbeat(force=True)
            result = fn(ctx)
            ctx.flush_log()
            ctx.heartbeat(force=True)
            self.jobs.complete(job.job_id, worker, result or {})
        except Cancelled:
            ctx.kill_all()
            self.jobs.fail(job.job_id, worker, "cancelled", retry=False)
        except StageError as e:
            ctx.kill_all()
            self.jobs.fail(job.job_id, worker, str(e), retry=e.retry, blocker=e.blocker)
        except Exception as e:  # unexpected: retryable up to max_attempts, with traceback kept
            ctx.kill_all()
            self.jobs.fail(job.job_id, worker, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-3000:]}", retry=True)
        finally:
            try:
                ctx.flush_log()
            except Exception:  # noqa: BLE001 - the log must never break job bookkeeping
                pass
            with self._lock:
                self._active.pop(job.job_id, None)
