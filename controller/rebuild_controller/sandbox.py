"""Process isolation for running untrusted programs (the user's original, AI-generated candidates, build scripts).

This is *damage limitation*, not a security boundary against a determined attacker. Exact guarantees and non-guarantees
per platform/mode are in docs/ISOLATION.md; every run records which limits applied and which ones triggered.

Windows (default ``integrity="low"``):
  * Job Object per run: KILL_ON_JOB_CLOSE, per-process and whole-job committed-memory caps, active-process cap,
    DIE_ON_UNHANDLED_EXCEPTION (no WER dialog hang), UI restrictions; no breakaway allowed. The process is created
    CREATE_SUSPENDED, assigned to the job, then resumed, so no child can start outside the job. Timeout, cancel and
    "main process exited" all end with TerminateJobObject (whole tree).
  * Low integrity token (own token duplicated, TokenIntegrityLevel = S-1-16-4096, CreateProcessAsUserW): mandatory
    "no write up" stops writes to medium-integrity objects (user profile, documents, the original install folder,
    HKCU). The work dir is labelled Low (OI)(CI) so it stays writable.
  * ``integrity="appcontainer"`` (opt-in): AppContainer with zero capabilities -> no network (incl. loopback) and no
    access to user files that do not grant ALL APPLICATION PACKAGES; the program dir is staged into the work dir.
  * ``integrity="medium"``: job + env scrubbing only; must carry an explicit ``downgrade_reason`` which is recorded.
  * Only the three std handles are inherited (PROC_THREAD_ATTRIBUTE_HANDLE_LIST).

POSIX: new session/process group (killpg on timeout/exit), RLIMIT_DATA / RLIMIT_CORE / RLIMIT_FSIZE / RLIMIT_CPU,
scrubbed environment, output caps. No filesystem or network confinement.

Network is NOT blocked except in the Windows ``appcontainer`` mode.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"

MiB = 1024 * 1024
GiB = 1024 * MiB

NETWORK_OPEN = "open: not blocked (no firewall rules are installed; that needs administrator rights)"
NETWORK_BLOCKED_APPCONTAINER = "blocked: AppContainer without capabilities (no internetClient/privateNetwork, loopback denied)"


class SandboxError(RuntimeError):
    """The isolated process could not be started with the requested isolation."""


class OriginalExecutionNotPermitted(PermissionError):
    """Executing the user's original program was requested without recorded consent."""


@dataclass
class IsolationPolicy:
    integrity: str = "low"                  # windows: low | medium | appcontainer ; posix: ignored (recorded)
    downgrade_reason: str | None = None     # required when integrity == "medium"
    allow_downgrade: bool = False           # if low IL cannot be set up on this host, fall back to medium (recorded)
    wall_time_s: float | None = 60.0
    process_memory_bytes: int | None = 1 * GiB
    job_memory_bytes: int | None = 2 * GiB
    max_processes: int | None = 32
    stdout_cap_bytes: int = 8 * MiB
    stderr_cap_bytes: int = 8 * MiB
    ui_restrictions: str = "strict"         # strict | interactive | none
    kill_leftovers_on_exit: bool = True     # after the main process exits, terminate anything it left running
    env_passthrough: tuple[str, ...] = ()   # host env names copied verbatim (toolchains); never secrets
    extra_path: tuple[str, ...] = ()
    redirect_profile: bool = True           # TEMP/TMP/HOME/USERPROFILE/APPDATA/LOCALAPPDATA -> work dir
    cpu_seconds: int | None = None          # POSIX RLIMIT_CPU
    max_file_bytes: int | None = 4 * GiB    # POSIX RLIMIT_FSIZE

    def __post_init__(self) -> None:
        if self.integrity not in ("low", "medium", "appcontainer"):
            raise ValueError(f"unknown isolation integrity {self.integrity!r} (low|medium|appcontainer)")
        if self.integrity == "medium" and not (self.downgrade_reason or "").strip():
            raise ValueError("isolation downgrade to medium integrity needs an explicit downgrade_reason (it is recorded)")
        if self.ui_restrictions not in ("strict", "interactive", "none"):
            raise ValueError("ui_restrictions must be strict|interactive|none")

    @classmethod
    def from_spec(cls, spec: dict[str, Any] | None, **defaults: Any) -> "IsolationPolicy":
        """Build from a launch/scenario ``isolation`` dict: {integrity, reason, memory_mb, job_memory_mb, max_processes,
        stdout_cap_bytes, stderr_cap_bytes, network: "blocked" (=appcontainer), allow_downgrade}."""
        kw = dict(defaults)
        spec = spec or {}
        if spec.get("network") == "blocked":
            kw["integrity"] = "appcontainer"
        if "integrity" in spec:
            kw["integrity"] = spec["integrity"]
        if spec.get("reason") or spec.get("downgrade_reason"):
            kw["downgrade_reason"] = spec.get("reason") or spec.get("downgrade_reason")
        for key, attr, mul in (("memory_mb", "process_memory_bytes", MiB), ("job_memory_mb", "job_memory_bytes", MiB),
                               ("max_processes", "max_processes", 1), ("stdout_cap_bytes", "stdout_cap_bytes", 1),
                               ("stderr_cap_bytes", "stderr_cap_bytes", 1)):
            if spec.get(key) is not None:
                kw[attr] = int(spec[key]) * mul
        if "allow_downgrade" in spec:
            kw["allow_downgrade"] = bool(spec["allow_downgrade"])
        return cls(**kw)

    def limits_dict(self) -> dict[str, Any]:
        return {"wall_time_s": self.wall_time_s, "process_memory_bytes": self.process_memory_bytes, "job_memory_bytes": self.job_memory_bytes,
                "max_processes": self.max_processes, "stdout_cap_bytes": self.stdout_cap_bytes, "stderr_cap_bytes": self.stderr_cap_bytes,
                "ui_restrictions": self.ui_restrictions, "cpu_seconds": self.cpu_seconds, "max_file_bytes": self.max_file_bytes}


@dataclass
class RunResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cancelled: bool
    duration_s: float
    stdout_truncated: bool
    stderr_truncated: bool
    triggered: list[str]
    limits: dict[str, Any]
    isolation: dict[str, Any]
    processes_total: int | None = None
    leftovers_killed: int = 0
    argv: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("stdout"); d.pop("stderr")
        return d


# ---------------------------------------------------------------------------------------------------------- environment
_WIN_KEEP = ("SystemRoot", "windir", "SystemDrive", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
             "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "OS", "ProgramData", "ProgramFiles",
             "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "PUBLIC")
_POSIX_KEEP = ("LANG", "LC_ALL", "TZ", "TERM")


def system_path_dirs() -> list[str]:
    if IS_WINDOWS:
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return [os.path.join(root, "System32"), root, os.path.join(root, "System32", "Wbem")]
    return ["/usr/local/bin", "/usr/bin", "/bin"]


def build_env(work: Path, *, program_dirs: list[str] | tuple[str, ...] = (), policy: IsolationPolicy | None = None,
              declared: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal allow-listed environment. Nothing from the host leaks except the allowlist and ``policy.env_passthrough``."""
    policy = policy or IsolationPolicy()
    host = os.environ
    env: dict[str, str] = {}
    keep = _WIN_KEEP if IS_WINDOWS else _POSIX_KEEP
    lower = {k.lower(): k for k in host}
    for name in keep + tuple(policy.env_passthrough):
        real = lower.get(name.lower())
        if real is not None:
            env[name] = host[real]
    path = [str(p) for p in program_dirs if p] + list(policy.extra_path) + system_path_dirs()
    seen: set[str] = set()
    env["PATH"] = os.pathsep.join(p for p in path if not (p.lower() in seen or seen.add(p.lower())))
    if policy.redirect_profile:
        home = work / ".home"
        dirs = {"home": home, "tmp": home / "tmp", "appdata": home / "AppData" / "Roaming", "local": home / "AppData" / "Local"}
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        env.update({"HOME": str(home), "TEMP": str(dirs["tmp"]), "TMP": str(dirs["tmp"]), "TMPDIR": str(dirs["tmp"])})
        if IS_WINDOWS:
            drive, rest = os.path.splitdrive(str(home))
            env.update({"USERPROFILE": str(home), "APPDATA": str(dirs["appdata"]), "LOCALAPPDATA": str(dirs["local"]),
                        "HOMEDRIVE": drive, "HOMEPATH": rest or "\\"})
        else:
            env.update({"XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local" / "share")})
    if not IS_WINDOWS:
        env.setdefault("LANG", "C.UTF-8")
    for k, v in (declared or {}).items():
        env[str(k)] = str(v)
    return env


# ---------------------------------------------------------------------------------------------------------- public API
def prepare_work_dir(work: Path, policy: IsolationPolicy) -> dict[str, Any]:
    """Create the work dir and make it writable for the isolated process (low label / AppContainer grant)."""
    work.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS and policy.integrity == "low":
        _win().label_low(work)
        return {"work_dir_label": "Low (OI)(CI) NW"}
    if IS_WINDOWS and policy.integrity == "appcontainer":
        _win().grant_appcontainer(work, full=True)
        return {"work_dir_label": "AppContainer SID granted (OI)(CI) full"}
    return {"work_dir_label": None}


def run(argv: list[str], *, work: Path, cwd: Path | None = None, policy: IsolationPolicy | None = None, env: dict[str, str] | None = None,
        stdin: bytes | None = None, poll: Callable[[], None] | None = None, label: str = "") -> RunResult:
    """Run ``argv`` isolated, wait for it (bounded), return captured (capped) output and what was enforced.

    ``env`` is the complete child environment (use :func:`build_env`); ``None`` means build_env(work).
    ``poll`` is called about once a second; if it raises (e.g. job cancelled), the whole tree is killed and it re-raises.
    """
    policy = policy or IsolationPolicy()
    if env is None:
        env = build_env(work, policy=policy)
    cwd = cwd or work
    if IS_WINDOWS:
        return _win().run(list(argv), work=work, cwd=cwd, policy=policy, env=env, stdin=stdin, poll=poll, label=label)
    return _posix_run(list(argv), work=work, cwd=cwd, policy=policy, env=env, stdin=stdin, poll=poll)


def spawn(argv: list[str], *, work: Path, cwd: Path | None = None, policy: IsolationPolicy | None = None,
          env: dict[str, str] | None = None) -> "SandboxedProcess":
    """Start a long-running isolated process (previews). No output capture; std handles are not inherited."""
    policy = policy or IsolationPolicy(wall_time_s=None, ui_restrictions="interactive")
    if env is None:
        env = build_env(work, policy=policy)
    cwd = cwd or work
    if IS_WINDOWS:
        return _win().spawn(list(argv), work=work, cwd=cwd, policy=policy, env=env)
    return _posix_spawn(list(argv), cwd=cwd, policy=policy, env=env)


def describe_host() -> dict[str, Any]:
    """What isolation this host offers (probed, not assumed). Used by /isolation and doctor-style UI."""
    out: dict[str, Any] = {"platform": sys.platform, "network_default": NETWORK_OPEN}
    if IS_WINDOWS:
        w = _win()
        out["modes"] = {"low": w.probe("low"), "appcontainer": w.probe("appcontainer"), "medium": {"available": True}}
        out["default_mode"] = "low"
        out["network_blocking"] = "available only in 'appcontainer' mode" if out["modes"]["appcontainer"].get("available") else "not available without administrator rights"
    else:
        out["modes"] = {"posix_rlimits": {"available": True}}
        out["default_mode"] = "posix_rlimits"
        out["network_blocking"] = "not available (no unprivileged mechanism used)"
    return out


def program_staging_needed(policy: IsolationPolicy) -> bool:
    return IS_WINDOWS and policy.integrity == "appcontainer"


def stage_program_dir(src: Path, work: Path, *, max_bytes: int = 1 * GiB) -> Path:
    """AppContainer cannot read arbitrary user folders: copy the program dir into the (granted) work area."""
    total = 0
    for p in src.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            if total > max_bytes:
                raise SandboxError(f"program folder larger than {max_bytes // MiB} MiB; too big to stage for AppContainer mode")
    dest = work / ".program"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


class SandboxedProcess:
    """Handle on a spawned isolated process tree."""

    pid: int

    def poll(self) -> int | None:  # pragma: no cover - interface
        raise NotImplementedError

    def kill_tree(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> int | None:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------------------------------------- output pumping
class _Pump(threading.Thread):
    def __init__(self, read: Callable[[int], bytes], cap: int):
        super().__init__(daemon=True)
        self.read, self.cap = read, cap
        self.chunks: list[bytes] = []
        self.size = 0
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                b = self.read(65536)
                if not b:
                    break
                room = self.cap - self.size
                if len(b) > room:
                    self.truncated = True
                    b = b[:max(0, room)]
                if b:
                    self.chunks.append(b); self.size += len(b)
                # past the cap we keep draining (so the child never blocks on a full pipe) but drop data
        except (OSError, ValueError):
            pass

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


def _feed(write: Callable[[bytes], Any], close: Callable[[], Any], data: bytes | None) -> threading.Thread:
    def go():
        try:
            if data:
                write(data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                close()
            except OSError:
                pass
    t = threading.Thread(target=go, daemon=True); t.start()
    return t


# ---------------------------------------------------------------------------------------------------------- POSIX
def _posix_preexec(policy: IsolationPolicy):
    def fn():  # runs in the child after fork
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        mem = policy.process_memory_bytes
        if mem:
            # RLIMIT_DATA (private writable memory), not RLIMIT_AS: runtimes such as .NET and wine reserve huge PROT_NONE ranges
            resource.setrlimit(getattr(resource, "RLIMIT_DATA", resource.RLIMIT_AS), (mem, mem))
        if policy.cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds))
        if policy.max_file_bytes:
            resource.setrlimit(resource.RLIMIT_FSIZE, (policy.max_file_bytes, policy.max_file_bytes))
    return fn


def _posix_isolation(policy: IsolationPolicy) -> dict[str, Any]:
    return {"platform": sys.platform, "mode": "posix_rlimits", "integrity": None, "job_object": False,
            "process_group": True, "env": "allowlist", "network": NETWORK_OPEN, "filesystem": "not confined (runs as the current user)",
            "downgrade": None, "rlimits": ["RLIMIT_DATA", "RLIMIT_CORE=0", "RLIMIT_FSIZE"] + (["RLIMIT_CPU"] if policy.cpu_seconds else [])}


def _killpg(pid: int) -> None:
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _posix_run(argv, *, work, cwd, policy, env, stdin, poll) -> RunResult:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=_posix_preexec(policy), close_fds=True)
    except OSError as e:
        raise SandboxError(f"could not start {argv[0]}: {e}") from e
    po, pe = _Pump(proc.stdout.read, policy.stdout_cap_bytes), _Pump(proc.stderr.read, policy.stderr_cap_bytes)
    po.start(); pe.start()
    _feed(proc.stdin.write, proc.stdin.close, stdin)
    timed_out = cancelled = False
    triggered: list[str] = []
    try:
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if poll:
                try:
                    poll()
                except BaseException:
                    cancelled = True
                    _killpg(proc.pid); proc.wait(timeout=10)
                    raise
            if policy.wall_time_s is not None and time.monotonic() - start > policy.wall_time_s:
                timed_out = True
                triggered.append("wall_time")
                _killpg(proc.pid)
                proc.wait(timeout=10)
                break
    finally:
        if policy.kill_leftovers_on_exit or timed_out or cancelled:
            _killpg(proc.pid)   # anything still in the process group (children that did not setsid away)
        po.join(5); pe.join(5)
    if po.truncated:
        triggered.append("output_cap:stdout")
    if pe.truncated:
        triggered.append("output_cap:stderr")
    if proc.returncode is not None and proc.returncode < 0:
        import signal
        if -proc.returncode == getattr(signal, "SIGXCPU", -1):
            triggered.append("cpu_time")
        if -proc.returncode == getattr(signal, "SIGXFSZ", -1):
            triggered.append("file_size")
    return RunResult(None if timed_out else proc.returncode, po.data, pe.data, timed_out, cancelled, time.monotonic() - start,
                     po.truncated, pe.truncated, triggered, policy.limits_dict(), _posix_isolation(policy), argv=argv)


class _PosixProcess(SandboxedProcess):
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.pid = proc.pid

    def poll(self):
        return self.proc.poll()

    def wait(self, timeout=None):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill_tree(self):
        _killpg(self.proc.pid)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _posix_spawn(argv, *, cwd, policy, env) -> SandboxedProcess:
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, start_new_session=True,
                                preexec_fn=_posix_preexec(policy), close_fds=True)
    except OSError as e:
        raise SandboxError(f"could not start {argv[0]}: {e}") from e
    p = _PosixProcess(proc)
    p.isolation = _posix_isolation(policy)  # type: ignore[attr-defined]
    return p


# ---------------------------------------------------------------------------------------------------------- Windows
_WIN_IMPL = None


def _win():
    global _WIN_IMPL
    if _WIN_IMPL is None:
        from . import _sandbox_win
        _WIN_IMPL = _sandbox_win
    return _WIN_IMPL
