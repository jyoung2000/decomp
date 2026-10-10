"""Rizin native backend: one persistent rzpipe session per module, typed operations, evidence output.

Design notes
- ``_RizinPipe`` subclasses ``rzpipe.OpenBase`` and replaces its process transport: rzpipe's stock reader busy-loops
  without a deadline and never notices a dead child. Ours enforces a per-command deadline, an output cap
  (``limits.max_subprocess_output_bytes``), crash detection (EOF / exit) and a cancellation poll; the child runs in its
  own process group/session so the whole tree can be killed.
- ``RizinSession`` owns one ``_RizinPipe`` for one module file. A single re-entrant owner lock serialises every command,
  so concurrent callers can never interleave seeks/commands. Commands always use temporary seeks (``cmd @ 0x...``) built
  from integers, never from caller text. A crashed child is detected and re-created (analysis is replayed lazily).
- Callers never pass raw rizin commands. The only raw entry point is ``admin_raw`` (dangerous, not an ``op_*`` method,
  therefore unreachable through ``BackendAdapter.call`` and never exposed to models).
- Every operation returns ``OperationResult`` and stores evidence (producer ``rizin``); inputs (module sha256, rizin
  version+commit, analysis settings, decompiler id) are the cache key. Binary-derived content is ``meta.untrusted``.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rzpipe.open_base import OpenBase

from ..adapters.contract import Availability, BackendAdapter, BackendInfo, Operation, OperationResult, ToolProbe
from ..config import Limits, Settings, get_settings
from ..ids import sha256_file
from ..jobs.runner import kill_tree
from ..paths import resolve_final
from . import native
from . import re_annotations as annotations

RIZIN_PINNED = "v0.9.1"
RIZIN_STATIC_SHA256 = "9102249a9f0b6319c5334a2e5cf8d9cc3f2035e1d3def027c41f6a90f647e8cf"
RIZIN_LICENSE = "LGPL-3.0"
RIZIN_SOURCE = "https://github.com/rizinorg/rizin"
RZ_GHIDRA_PINNED = "v0.9.0"
RZ_GHIDRA_LICENSE = "LGPL-3.0 (plugin) + Apache-2.0 (Ghidra decompiler)"
RZ_GHIDRA_SOURCE = "https://github.com/rizinorg/rz-ghidra"

# "passes": post-analysis passes from rizin_passes (pdata function starts, import-thunk names, shipped signature packs).
DEFAULT_ANALYSIS = {"command": "aaa", "analysis.timeout": 300, "passes": ["sigpacks", "pdata", "relocptrs", "thunks"]}
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_COMMAND_TIMEOUT = 120.0
LOAD_TIMEOUT = 60.0
MAX_STRINGS = 20_000
MAX_FUNCTIONS = 50_000
MAX_STRING_CHARS = 1_000

# Caller-supplied symbol names: identifier-ish only. Shell/rizin metacharacters (; | & ` $ < > ( ) ' " \ space * [ ] # ~ = %
# ! { } , and newlines) are rejected. '?' and '@' are allowed because MSVC-mangled names need them; names are never sent
# to rizin or a shell anyway (they are resolved to addresses in Python first).
SYMBOL_RE = re.compile(r"^[A-Za-z_.?@][A-Za-z0-9_.?@:\-]{0,255}$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
DEC_RE = re.compile(r"^[0-9]{1,20}$")


class RizinError(RuntimeError):
    pass


class RizinTimeout(RizinError):
    pass


class RizinCrashed(RizinError):
    pass


class InvalidTarget(ValueError):
    pass


# =============================================================================================== tool discovery
@dataclass
class RizinTool:
    exe: Path
    version: str | None = None
    commit: str | None = None
    static: bool = False
    lib_plugins: Path | None = None
    user_plugins: Path | None = None
    ghidra_plugin: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)   # <prefix>/share/rebuild-studio/build-manifest.json (source builds)
    error: str = ""

    @property
    def prefix(self) -> Path:
        return self.exe.parent.parent

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.env)
        return env


def _exe_name() -> str:
    return "rizin.exe" if os.name == "nt" else "rizin"


def _is_static_elf(path: Path) -> bool:
    """True if an ELF executable has no PT_INTERP (statically linked). Static rizin cannot dlopen rz-ghidra."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(64)
            if hdr[:4] != b"\x7fELF" or hdr[4] != 2:
                return False
            phoff, = struct.unpack_from("<Q", hdr, 0x20)
            phentsize, phnum = struct.unpack_from("<HH", hdr, 0x36)
            f.seek(phoff)
            ph = f.read(phentsize * phnum)
        return not any(struct.unpack_from("<I", ph, i * phentsize)[0] == 3 for i in range(phnum))
    except (OSError, struct.error, IndexError):
        return False


def _plugin_candidates(tools_dir: Path) -> list[Path]:
    """Ordered candidate rizin executables. An install that carries rz-ghidra beats the static build."""
    out: list[Path] = []
    env = os.environ.get("REBUILD_STUDIO_RIZIN")
    if env:
        out.append(Path(env))
    out.append(tools_dir / "rizin" / _exe_name())                         # Windows: Cutter's bundled rizin + rz-ghidra (pinned)
    out.append(tools_dir / "rizin" / "bin" / _exe_name())                 # pinned static release (integrity-checked)
    out.append(tools_dir / "rizin-src-install" / "bin" / _exe_name())     # source build (D1), carries rz-ghidra
    which = shutil.which("rizin")
    if which:
        out.append(Path(which))
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        out.append(base / "RebuildStudio" / "tools" / "rizin" / "rizin.exe")
        out.append(base / "RebuildStudio" / "tools" / "rizin" / "bin" / "rizin.exe")
    seen: set[str] = set()
    uniq = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p)); uniq.append(p)
    return uniq


_TOOL_CACHE: dict[tuple[str, float], RizinTool] = {}
_TOOL_LOCK = threading.Lock()


def inspect_rizin(exe: Path) -> RizinTool:
    """Version, commit, plugin dirs and rz-ghidra presence for one rizin executable (cached by path+mtime)."""
    try:
        key = (str(exe), exe.stat().st_mtime)
    except OSError as e:
        return RizinTool(exe=exe, error=f"{type(e).__name__}: {e}")
    with _TOOL_LOCK:
        if key in _TOOL_CACHE:
            return _TOOL_CACHE[key]
    tool = RizinTool(exe=exe, static=_is_static_elf(exe))
    if not tool.static and os.name != "nt":
        libdirs = [p for p in (tool.prefix / "lib", tool.prefix / "lib64", tool.prefix / "lib" / "x86_64-linux-gnu") if p.is_dir()]
        if libdirs:
            prev = os.environ.get("LD_LIBRARY_PATH", "")
            tool.env["LD_LIBRARY_PATH"] = os.pathsep.join([str(p) for p in libdirs] + ([prev] if prev else []))
    try:
        r = subprocess.run([str(exe), "-v"], capture_output=True, timeout=20, env=tool.child_env())
        text = r.stdout.decode("utf-8", "replace")
        m = re.search(r"rizin\s+(\S+)", text)
        tool.version = m.group(1) if m else None
        m = re.search(r"commit:\s*([0-9a-fA-F]+)", text)
        tool.commit = m.group(1) if m else None
        if not tool.version:
            tool.error = f"could not parse `rizin -v` output: {text[:200]!r}"
        for var, attr in (("RZ_LIB_PLUGINS", "lib_plugins"), ("RZ_USER_PLUGINS", "user_plugins")):
            r = subprocess.run([str(exe), "-H", var], capture_output=True, timeout=20, env=tool.child_env())
            val = r.stdout.decode("utf-8", "replace").strip()
            if val:
                setattr(tool, attr, Path(val))
        for d in (tool.lib_plugins, tool.user_plugins):
            if d and d.is_dir():
                hits = sorted(p for p in d.iterdir() if "ghidra" in p.name.lower() and p.suffix in (".so", ".dll", ".dylib"))
                core = [p for p in hits if p.name.lower().startswith(("core_ghidra", "libcore_ghidra"))]
                if hits:
                    tool.ghidra_plugin = (core or hits)[0]
                    break
        mf = tool.prefix / "share" / "rebuild-studio" / "build-manifest.json"
        if mf.is_file():
            try:
                tool.manifest = json.loads(mf.read_text("utf-8"))
            except ValueError:
                tool.manifest = {}
    except (OSError, subprocess.SubprocessError) as e:
        tool.error = f"{type(e).__name__}: {e}"
    with _TOOL_LOCK:
        _TOOL_CACHE[key] = tool
    return tool


_SHA_CACHE: dict[tuple[str, float, int], str] = {}


def _cached_sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _SHA_CACHE:
        _SHA_CACHE[key] = sha256_file(path)
    return _SHA_CACHE[key]


def find_rizin(settings: Settings | None = None) -> RizinTool | None:
    """Pick the rizin to use: an install with a loadable rz-ghidra first, then the first working rizin."""
    settings = settings or get_settings()
    working: list[RizinTool] = []
    override = os.environ.get("REBUILD_STUDIO_RIZIN")
    for cand in _plugin_candidates(Path(settings.tools_dir)):
        if not cand.is_file():
            continue
        tool = inspect_rizin(cand)
        if tool.version:
            if override and str(cand) == override:
                return tool
            working.append(tool)
    for t in working:
        if t.ghidra_plugin and not t.static:
            return t
    return working[0] if working else None


# =============================================================================================== pipe transport
class _RizinPipe(OpenBase):
    """rzpipe session over a child ``rizin -q0`` with deadlines, an output cap, crash detection and tree kill."""

    def __init__(self, tool: RizinTool, target: Path, *, output_cap: int, load_timeout: float = LOAD_TIMEOUT,
                 poll: Callable[[], None] | None = None):
        # OpenBase.__init__ is intentionally not called: it probes RZ_PIPE_* / rzlang, which only apply when *we* are
        # running inside rizin. We set the attributes OpenBase.cmd()/cmdj() rely on.
        self._cmd_timeout_secs = -1
        self._async = False
        self.uri = str(target)
        self.output_cap = output_cap
        self.last_truncated = False
        self._next_timeout = DEFAULT_COMMAND_TIMEOUT
        self._next_poll: Callable[[], None] | None = None
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._pending = b""
        self._stderr: collections.deque[bytes] = collections.deque(maxlen=64)
        argv = [str(tool.exe), "-q0", "-N",
                "-e", "scr.color=0", "-e", "scr.utf8=false", "-e", "scr.interactive=false",
                "-e", "scr.prompt=false", "-e", "cfg.fortunes=false", "-e", "scr.columns=160"]
        argv.append(str(target))
        kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                  "bufsize": 0, "env": tool.child_env()}
        sleigh = tool.ghidra_plugin.parent / "rz_ghidra_sleigh" if tool.ghidra_plugin else None
        if sleigh is not None and sleigh.is_dir() and "SLEIGHHOME" not in kwargs["env"]:
            # Windows builds (Cutter's bundled rizin) ship the sleigh specs next to the plugin without configuring them.
            # `-e ghidra.sleighhome=` collides with the plugin's own variable; the SLEIGHHOME env var is what rz-ghidra reads.
            kwargs["env"]["SLEIGHHOME"] = str(sleigh)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(argv, **kwargs)
        self.pid = self.process.pid
        self._cmd = self._cmd_bounded
        threading.Thread(target=self._pump_stdout, name=f"rz-out-{self.pid}", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name=f"rz-err-{self.pid}", daemon=True).start()
        try:
            self._read_reply(load_timeout, poll)   # startup banner + the initial \x00
        except BaseException:
            self.kill()
            raise

    # -- reader threads -------------------------------------------------
    def _pump_stdout(self) -> None:
        fd = self.process.stdout.fileno()
        try:
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                self._chunks.put(b)
        except OSError:
            pass
        self._chunks.put(None)

    def _pump_stderr(self) -> None:
        fd = self.process.stderr.fileno()
        try:
            while True:
                b = os.read(fd, 65536)
                if not b:
                    break
                self._stderr.append(b[-4096:])
        except OSError:
            pass

    def stderr_tail(self, n: int = 2000) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace")[-n:]

    # -- protocol -------------------------------------------------------
    def alive(self) -> bool:
        return hasattr(self, "process") and self.process.poll() is None

    def _read_reply(self, timeout: float, poll: Callable[[], None] | None) -> bytes:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        truncated = False
        last_poll = 0.0   # poll immediately, then every 0.5s (StageContext.heartbeat rate-limits itself)
        pending, self._pending = self._pending, b""
        while True:
            if pending:
                chunk: bytes | None = pending
                pending = b""
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.kill()
                    raise RizinTimeout(f"rizin did not answer within {timeout:.0f}s (process killed)")
                try:
                    chunk = self._chunks.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    chunk = b""
            if poll and time.monotonic() - last_poll >= 0.5:
                last_poll = time.monotonic()
                try:
                    poll()
                except BaseException:
                    self.kill()
                    raise
            if chunk is None:
                code = self.process.poll()
                raise RizinCrashed(f"rizin exited (code {code}); stderr: {self.stderr_tail(400)!r}")
            if not chunk:
                if self.process.poll() is not None and self._chunks.empty():
                    raise RizinCrashed(f"rizin exited (code {self.process.returncode}); stderr: {self.stderr_tail(400)!r}")
                continue
            idx = chunk.find(b"\x00")
            part = chunk if idx < 0 else chunk[:idx]
            if not truncated:
                room = self.output_cap - len(buf)
                if len(part) > room:
                    buf += part[:max(0, room)]
                    truncated = True
                else:
                    buf += part
            if idx >= 0:
                self._pending = chunk[idx + 1:]
                self.last_truncated = truncated
                return bytes(buf)

    def _cmd_bounded(self, cmd: str) -> str:
        if "\n" in cmd or "\r" in cmd or "\x00" in cmd:
            raise ValueError("rizin commands must be a single line")
        if not self.alive():
            raise RizinCrashed(f"rizin is not running (code {self.process.returncode})")
        try:
            self.process.stdin.write((cmd + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise RizinCrashed(f"rizin stdin closed: {e}") from None
        return self._read_reply(self._next_timeout, self._next_poll).decode("utf-8", "replace")

    def execute(self, cmd: str, *, timeout: float, poll: Callable[[], None] | None) -> tuple[str, bool]:
        """Run one command through rzpipe's ``cmd`` with our deadline/cap. Returns (text, truncated)."""
        self._next_timeout, self._next_poll = timeout, poll
        self.last_truncated = False
        text = self.cmd(cmd)
        return (text or ""), self.last_truncated

    def kill(self) -> None:
        if hasattr(self, "process"):
            kill_tree(self.process)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                except OSError:
                    pass
            for s in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    def quit(self) -> None:  # rzpipe API
        if self.alive():
            try:
                self.process.stdin.write(b"q!!\n"); self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.kill()


# =============================================================================================== JSON helpers
def parse_json(text: str, truncated: bool) -> tuple[Any, bool]:
    """Parse rizin JSON. If the output was cut by the cap, salvage the complete leading items of a JSON list."""
    s = text.strip()
    if not s:
        return None, truncated
    try:
        return json.loads(s), truncated
    except ValueError:
        pass
    if s.startswith("["):
        dec = json.JSONDecoder()
        items: list[Any] = []
        i = 1
        n = len(s)
        while i < n:
            while i < n and s[i] in " \t\r\n,":
                i += 1
            if i >= n or s[i] == "]":
                break
            try:
                obj, i = dec.raw_decode(s, i)
            except ValueError:
                break
            items.append(obj)
        return items, True
    raise RizinError(f"rizin returned invalid JSON ({len(s)} bytes, starts {s[:120]!r})")


# =============================================================================================== session
class RizinSession:
    """One persistent rizin process for one module file, guarded by a single owner lock."""

    def __init__(self, tool: RizinTool, path: Path, *, sha256: str | None, limits: Limits,
                 analysis: dict[str, Any] | None = None):
        self.tool = tool
        self.path = path
        self.sha256 = sha256
        self.limits = limits
        self.analysis_settings = dict(analysis or DEFAULT_ANALYSIS)
        self.lock = threading.RLock()
        self.owner: int | None = None
        self.last_used = time.monotonic()
        self.generation = 0
        self.commands_run = 0
        self._pipe: _RizinPipe | None = None
        self._analyzed: dict[str, Any] | None = None
        self._analysis_report: dict[str, Any] | None = None
        self._caps: dict[str, Any] | None = None
        self._ranges: list[tuple[int, int, str]] | None = None
        self._functions: list[dict[str, Any]] | None = None
        self._closed = False
        # Re-applies persisted user/model annotations (renames, types, structs) after every (re-)analysis, so a
        # re-created rizin process never serves un-annotated output. Set by RizinBackend for case-scoped sessions.
        self.annotator: Callable[["RizinSession", Callable[[], None] | None], Any] | None = None
        self.annotation_report: dict[str, Any] | None = None
        # A verified PDB for this file ({"path", "key", "source"}); loaded with idp before every (re-)analysis.
        self.pdb: dict[str, Any] | None = None

    # -- ownership ------------------------------------------------------
    @contextmanager
    def owned(self):
        with self.lock:
            prev = self.owner
            self.owner = threading.get_ident()
            self.last_used = time.monotonic()
            try:
                yield self
            finally:
                self.owner = prev
                self.last_used = time.monotonic()

    @property
    def pid(self) -> int | None:
        p = self._pipe
        return p.pid if p is not None and p.alive() else None

    def busy(self) -> bool:
        return self.owner is not None

    # -- lifecycle ------------------------------------------------------
    def _ensure_pipe(self, poll: Callable[[], None] | None) -> _RizinPipe:
        if self._closed:
            raise RizinError("session closed")
        if self._pipe is None or not self._pipe.alive():
            if self._pipe is not None:
                self._pipe.kill()
            self._pipe = None
            self._analyzed = None
            self._caps = None
            self._functions = None
            self._pipe = _RizinPipe(self.tool, self.path, output_cap=self.limits.max_subprocess_output_bytes, poll=poll)
            self.generation += 1
        return self._pipe

    def _drop(self) -> None:
        if self._pipe is not None:
            self._pipe.kill()
        self._pipe = None
        self._analyzed = None
        self._caps = None
        self._functions = None

    def close(self) -> None:
        with self.lock:
            if self._pipe is not None:
                self._pipe.quit()
            self._pipe = None
            self._analyzed = None
            self._closed = True

    def kill(self) -> None:
        """Cancellation: kill the rizin process tree immediately (does not wait for the lock)."""
        p = self._pipe
        if p is not None:
            p.kill()

    # -- command execution (internal; commands are built here from typed values only) --------------
    def _exec(self, cmd: str, *, timeout: float | None, poll: Callable[[], None] | None) -> tuple[str, bool]:
        pipe = self._ensure_pipe(poll)
        self.commands_run += 1
        try:
            return pipe.execute(cmd, timeout=timeout or DEFAULT_COMMAND_TIMEOUT, poll=poll)
        except RizinTimeout:
            self._drop()
            raise
        except RizinCrashed:
            self._drop()
            raise
        except BaseException:
            # cancellation (or anything else) mid-command leaves the protocol out of sync: drop the process.
            self._drop()
            raise

    def _run(self, cmd: str, *, json_out: bool = True, timeout: float | None = None,
             poll: Callable[[], None] | None = None, needs_analysis: bool = False) -> tuple[Any, bool]:
        """Run a command; on a crashed child, re-create the session (replaying analysis) and retry once."""
        with self.owned():
            for attempt in (0, 1):
                try:
                    if needs_analysis:
                        self._ensure_analyzed(poll)
                    text, truncated = self._exec(cmd, timeout=timeout, poll=poll)
                    if json_out:
                        return parse_json(text, truncated)
                    return text, truncated
                except RizinCrashed:
                    if attempt:
                        raise
        raise RizinError("unreachable")

    # -- capabilities -----------------------------------------------------
    def capabilities(self, poll: Callable[[], None] | None = None) -> dict[str, Any]:
        with self.owned():
            self._ensure_pipe(poll)
            if self._caps is None:
                plugins, _ = parse_json(self._exec("Lcj", timeout=30, poll=poll)[0], False)
                names = [p.get("name", "") for p in plugins or [] if isinstance(p, dict)]
                ghidra = any("ghidra" in n.lower() for n in names)
                help_pd, _ = self._exec("pd?", timeout=30, poll=poll)
                has_pdc = re.search(r"(^|\W)pdc(\W|$)", help_pd) is not None
                self._caps = {"core_plugins": names, "rz_ghidra_loaded": ghidra, "has_pdc": has_pdc}
            return dict(self._caps)

    def decompiler_id(self, poll: Callable[[], None] | None = None) -> str:
        caps = self.capabilities(poll)
        if caps["rz_ghidra_loaded"]:
            return "rz-ghidra(pdg)"
        return "pdc(pseudo)" if caps["has_pdc"] else "pdf+asm.pseudo(pseudo)"

    # -- analysis ----------------------------------------------------------
    def _ensure_analyzed(self, poll: Callable[[], None] | None) -> dict[str, Any]:
        if self._analyzed == self.analysis_settings and self._pipe is not None and self._pipe.alive():
            return self._analysis_report or {}
        s = self.analysis_settings
        cmd = s.get("command", "aaa")
        if cmd not in ("aa", "aaa", "aaaa"):
            raise ValueError(f"unsupported analysis command {cmd!r}")
        atimeout = int(s.get("analysis.timeout", 300))
        pdb_report = None
        if self.pdb:
            pp = str(Path(self.pdb["path"]).resolve()).replace("\\", "/")
            if '"' not in pp and not any(ord(c) < 32 for c in pp):
                self._exec(f'idp "{pp}"', timeout=300, poll=poll)
                pdb_report = {k: self.pdb.get(k) for k in ("key", "source", "name")}
        self._exec(f"e analysis.timeout={atimeout:d}", timeout=30, poll=poll)
        start = time.monotonic()
        self._exec(cmd, timeout=atimeout + 60, poll=poll)
        elapsed = time.monotonic() - start
        passes_report = None
        if s.get("passes"):
            from . import rizin_passes
            passes_report = rizin_passes.run_passes(self, list(s["passes"]), poll)
        self._analyzed = dict(s)
        self._functions = None
        self._analysis_report = {"settings": dict(s), "elapsed_seconds": round(elapsed, 3),
                                 "possibly_partial": bool(atimeout and elapsed >= atimeout), "generation": self.generation,
                                 "passes": passes_report, "passes_seconds": round(time.monotonic() - start - elapsed, 3),
                                 "pdb": pdb_report}
        if self.annotator is not None:
            self.annotation_report = self.annotator(self, poll)
            self._functions = None
        return self._analysis_report

    def analyze(self, poll: Callable[[], None] | None = None) -> dict[str, Any]:
        with self.owned():
            for attempt in (0, 1):
                try:
                    self._ensure_pipe(poll)
                    return dict(self._ensure_analyzed(poll))
                except RizinCrashed:
                    if attempt:
                        raise
        raise RizinError("unreachable")

    # -- typed queries (no caller text ever reaches rizin) -------------------
    def info(self, poll=None) -> tuple[Any, bool]:
        return self._run("iIj", poll=poll)

    def headers(self, poll=None) -> tuple[Any, bool]:
        return self._run("iHj", poll=poll)

    def imports(self, poll=None) -> tuple[Any, bool]:
        return self._run("iij", poll=poll)

    def exports(self, poll=None) -> tuple[Any, bool]:
        return self._run("iEj", poll=poll)

    def symbols(self, poll=None) -> tuple[Any, bool]:
        return self._run("isj", poll=poll)

    def sections(self, poll=None) -> tuple[Any, bool]:
        return self._run("iSj", poll=poll)

    def segments(self, poll=None) -> tuple[Any, bool]:
        return self._run("iSSj", poll=poll)

    def strings(self, poll=None) -> tuple[Any, bool]:
        return self._run("izzj", poll=poll, timeout=300)

    def entrypoints(self, poll=None) -> tuple[Any, bool]:
        return self._run("iej", poll=poll)

    def relocations(self, poll=None) -> tuple[Any, bool]:
        return self._run("irj", poll=poll, timeout=300)

    def all_xrefs(self, poll=None) -> tuple[Any, bool]:
        """Every cross reference rizin knows after analysis (axlj): [{from, to, type}]."""
        return self._run("axlj", poll=poll, needs_analysis=True, timeout=300)

    def functions(self, poll=None) -> tuple[list[dict[str, Any]], bool]:
        with self.owned():
            data, trunc = self._run("aflj", poll=poll, needs_analysis=True, timeout=300)
            data = [f for f in data or [] if isinstance(f, dict)]
            if not trunc:
                self._functions = data
            return data, trunc

    def cached_functions(self, poll=None) -> list[dict[str, Any]]:
        with self.owned():
            if self._functions is None or self._analyzed != self.analysis_settings:
                self.functions(poll)
            return list(self._functions or [])

    def function_info(self, addr: int, poll=None) -> dict[str, Any] | None:
        data, _ = self._run(f"afij @ {_addr(addr)}", poll=poll, needs_analysis=True)
        return data[0] if isinstance(data, list) and data else None

    def xrefs_to(self, addr: int, poll=None) -> tuple[Any, bool]:
        return self._run(f"axtj @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def xrefs_from(self, addr: int, poll=None) -> tuple[Any, bool]:
        return self._run(f"axfj @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def function_refs(self, addr: int, poll=None) -> tuple[Any, bool]:
        return self._run(f"afxj @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def callgraph(self, addr: int, poll=None) -> tuple[Any, bool]:
        return self._run(f"agc json @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def variables(self, addr: int, poll=None) -> tuple[Any, bool]:
        return self._run(f"afvlj @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def blocks(self, addr: int, poll=None) -> tuple[Any, bool]:
        return self._run(f"afbj @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def flag_at(self, addr: int, poll=None) -> dict[str, Any] | None:
        data, _ = self._run(f"fdj @ {_addr(addr)}", poll=poll)
        return data if isinstance(data, dict) and data.get("offset") == addr else None

    def disasm_linear(self, addr: int, *, count: int | None = None, length: int | None = None, poll=None) -> tuple[Any, bool]:
        """Linear disassembly from ``addr``: ``count`` instructions (pdj) or ``length`` bytes (pDj). Integers only."""
        if (count is None) == (length is None):
            raise ValueError("give exactly one of count or length")
        n = int(count if count is not None else length)  # type: ignore[arg-type]
        if n <= 0 or n > 65536:
            raise ValueError("count/length out of range")
        with self.owned():
            self._run("e asm.pseudo=false", json_out=False, poll=poll)
            return self._run(f"{'pdj' if count is not None else 'pDj'} {n:d} @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def types(self, kind: str, poll=None) -> tuple[Any, bool]:
        cmd = {"struct": "tsj", "union": "tuj", "enum": "tej", "typedef": "ttj"}[kind]
        return self._run(cmd, poll=poll)

    # -- annotation commands. Every argument is re-validated here against a strict charset (defence in depth: the
    #    callers validate first) so no caller text can ever add a command separator, pipe, temporary seek or quote.
    def rename_function(self, addr: int, name: str, poll=None) -> None:
        self._run(f"afn {_ident(name)} @ {_addr(addr)}", json_out=False, poll=poll)
        self._functions = None

    def set_prototype(self, addr: int, prototype: str, poll=None) -> None:
        self._run(f"afs {_proto(prototype)} @ {_addr(addr)}", json_out=False, poll=poll)
        self._functions = None

    def rename_variable(self, fn_addr: int, old: str, new: str, poll=None) -> None:
        self._run(f"afvn {_ident(new)} {_ident(old)} @ {_addr(fn_addr)}", json_out=False, poll=poll)

    def retype_variable(self, fn_addr: int, name: str, ctype: str, poll=None) -> None:
        self._run(f"afvt {_ident(name)} {_ctype(ctype)} @ {_addr(fn_addr)}", json_out=False, poll=poll)

    def set_flag(self, addr: int, name: str, poll=None) -> None:
        self._run(f"f {_ident(name)} 1 @ {_addr(addr)}", json_out=False, poll=poll)

    def remove_flag(self, name: str, poll=None) -> None:
        self._run(f"f- {_ident(name)}", json_out=False, poll=poll)

    def load_types_file(self, path: Path, poll=None) -> None:
        """Load C type declarations from a file the controller wrote (never a caller-chosen path)."""
        p = str(Path(path).resolve()).replace("\\", "/")
        if '"' in p or any(ord(c) < 32 for c in p):
            raise InvalidTarget("type file path contains characters rizin cannot quote")
        self._run(f'to "{p}"', json_out=False, poll=poll)

    def disasm(self, addr: int, poll=None) -> tuple[Any, bool]:
        with self.owned():
            self._run("e asm.pseudo=false", json_out=False, poll=poll)
            return self._run(f"pdfj @ {_addr(addr)}", poll=poll, needs_analysis=True)

    def decompile(self, addr: int, poll=None) -> dict[str, Any]:
        """rz-ghidra ``pdgj`` when loaded; otherwise rizin's pseudo output, labelled as such (never as decompilation)."""
        with self.owned():
            dec = self.decompiler_id(poll)
            if dec.startswith("rz-ghidra"):
                data, trunc = self._run(f"pdgj @ {_addr(addr)}", poll=poll, needs_analysis=True, timeout=300)
                code = data.get("code") if isinstance(data, dict) else None
                if code:
                    return {"decompiler": dec, "is_real_decompiler": True, "text": code, "truncated": trunc,
                            "rz_ghidra_plugin": str(self.tool.ghidra_plugin) if self.tool.ghidra_plugin else None}
                # fall through to pseudo output if the plugin produced nothing
                dec = "pdc(pseudo)" if self.capabilities(poll)["has_pdc"] else "pdf+asm.pseudo(pseudo)"
            if dec == "pdc(pseudo)":
                text, trunc = self._run(f"pdc @ {_addr(addr)}", json_out=False, poll=poll, needs_analysis=True)
            else:
                self._run("e asm.pseudo=true", json_out=False, poll=poll)
                try:
                    text, trunc = self._run(f"pdf @ {_addr(addr)}", json_out=False, poll=poll, needs_analysis=True)
                finally:
                    try:
                        self._run("e asm.pseudo=false", json_out=False, poll=poll)
                    except RizinError:
                        pass
            return {"decompiler": dec, "is_real_decompiler": False, "text": text, "truncated": trunc}

    def admin_raw(self, cmd: str, *, timeout: float | None = None) -> tuple[str, bool]:
        """DANGEROUS: run an arbitrary rizin command. For human admins/debugging only; never exposed to models."""
        if not isinstance(cmd, str) or "\n" in cmd or "\r" in cmd:
            raise ValueError("single-line command required")
        return self._run(cmd, json_out=False, timeout=timeout)

    # -- validation ------------------------------------------------------------
    def mapped_ranges(self, poll=None) -> list[tuple[int, int, str]]:
        with self.owned():
            if self._ranges is None:
                ranges: list[tuple[int, int, str]] = []
                secs, _ = self.sections(poll)
                for s in secs or []:
                    va, vs = s.get("vaddr"), s.get("vsize") or s.get("size")
                    if isinstance(va, int) and isinstance(vs, int) and vs > 0:
                        ranges.append((va, va + vs, f"section {s.get('name')}"))
                segs, _ = self.segments(poll)
                for s in segs or []:
                    va, vs = s.get("vaddr"), s.get("vsize") or s.get("size")
                    if isinstance(va, int) and isinstance(vs, int) and vs > 0:
                        ranges.append((va, va + vs, f"segment {s.get('name')}"))
                self._ranges = ranges
            return list(self._ranges)

    def validate_address(self, addr: int, poll=None) -> str:
        if not isinstance(addr, int) or isinstance(addr, bool) or addr < 0 or addr >= 1 << 64:
            raise InvalidTarget(f"address out of range: {addr!r}")
        for lo, hi, what in self.mapped_ranges(poll):
            if lo <= addr < hi:
                return what
        raise InvalidTarget(f"address 0x{addr:x} is not inside any mapped section or segment of {self.path.name}")

    def resolve(self, target: str | int, *, need_function: bool, poll=None) -> tuple[int, dict[str, Any] | None]:
        """Resolve a caller target (int, '0x..', decimal, or symbol name) to a validated address (+ function)."""
        addr = parse_address(target)
        if addr is None:
            name = validate_symbol(target)
            addr = self._lookup_name(name, poll)
            if addr is None:
                raise InvalidTarget(f"unknown symbol {name!r}")
        self.validate_address(addr, poll)
        fn = None
        if need_function:
            fn = self.function_info(addr, poll)
            if not fn:
                raise InvalidTarget(f"no analysed function contains 0x{addr:x}")
        return addr, fn

    def _lookup_name(self, name: str, poll=None) -> int | None:
        variants = [name, f"sym.{name}", f"fcn.{name}", f"sym.imp.{name}", f"dbg.{name}"]
        funcs = self.cached_functions(poll)
        by_name = {f.get("name"): f.get("offset") for f in funcs}
        for v in variants:
            if isinstance(by_name.get(v), int):
                return by_name[v]
        syms, _ = self.symbols(poll)
        for key in ("flagname", "name", "realname"):
            for s in syms or []:
                if s.get(key) in variants and isinstance(s.get("vaddr"), int) and s["vaddr"] > 0:
                    return s["vaddr"]
        # PE exports are flagged as sym.<module>_<name>
        for f in funcs:
            n = f.get("name") or ""
            if n.endswith("_" + name) and n.startswith("sym.") and isinstance(f.get("offset"), int):
                return f["offset"]
        return None


def _addr(addr: int) -> str:
    if not isinstance(addr, int) or isinstance(addr, bool) or addr < 0 or addr >= 1 << 64:
        raise InvalidTarget(f"address out of range: {addr!r}")
    return f"0x{addr:x}"


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CTYPE_RE = re.compile(r"^(?:(?:struct|union|enum|unsigned|signed|const|long|short|volatile)\s+){0,4}[A-Za-z_][A-Za-z0-9_]{0,63}"
                       r"(?:\s*\*{1,3})?$")
_PROTO_RE = re.compile(r"^[A-Za-z0-9_ *,()\[\].]{3,512}$")


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise InvalidTarget(f"invalid identifier {str(name)[:80]!r}: letters, digits and _ only, not starting with a digit")
    return name


def _ctype(t: str) -> str:
    if not isinstance(t, str) or not _CTYPE_RE.fullmatch(t.strip()):
        raise InvalidTarget(f"invalid C type {str(t)[:80]!r}")
    return " ".join(t.split())


def _proto(p: str) -> str:
    if not isinstance(p, str) or not _PROTO_RE.fullmatch(p.strip()) or p.count("(") != 1 or p.count(")") != 1:
        raise InvalidTarget(f"invalid prototype {str(p)[:80]!r}: expected e.g. 'int parse(char *s, int n)'")
    return " ".join(p.split())


def parse_address(target: Any) -> int | None:
    if isinstance(target, bool):
        raise InvalidTarget("boolean is not an address")
    if isinstance(target, int):
        return target
    if not isinstance(target, str):
        raise InvalidTarget(f"unsupported target type {type(target).__name__}")
    t = target.strip()
    if HEX_RE.fullmatch(t):
        return int(t, 16)
    if DEC_RE.fullmatch(t):
        return int(t, 10)
    return None


def validate_symbol(name: Any) -> str:
    if not isinstance(name, str) or not SYMBOL_RE.fullmatch(name):
        raise InvalidTarget(f"invalid symbol name {str(name)[:80]!r}: only letters, digits and _ . ? @ : - are allowed")
    return name


def normalize_target(target: Any) -> str:
    """Canonical string form for cache keys (validated, never executed)."""
    addr = parse_address(target)
    return f"0x{addr:x}" if addr is not None else validate_symbol(target)


# =============================================================================================== pool
class SessionPool:
    """Sessions keyed by (resolved path, sha256). An idle reaper closes sessions unused for ``idle_timeout`` seconds."""

    def __init__(self, limits: Limits, *, idle_timeout: float = DEFAULT_IDLE_TIMEOUT):
        self.limits = limits
        self.idle_timeout = idle_timeout
        self._sessions: dict[tuple[str, str | None, str | None], RizinSession] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    def get(self, tool: RizinTool, path: Path, sha256: str | None, analysis: dict[str, Any] | None = None,
            scope: str | None = None) -> RizinSession:
        """``scope`` separates sessions on the same file that carry different annotations (one per case module)."""
        key = (str(path), sha256, scope)
        with self._lock:
            s = self._sessions.get(key)
            if s is None or s._closed or s.tool.exe != tool.exe:
                if s is not None:
                    s.kill()   # rizin binary changed (e.g. rz-ghidra install appeared): retire the old process
                s = RizinSession(tool, path, sha256=sha256, limits=self.limits, analysis=analysis)
                self._sessions[key] = s
            self._start_reaper()
            return s

    def sessions(self) -> list[RizinSession]:
        with self._lock:
            return list(self._sessions.values())

    def _start_reaper(self) -> None:
        if self._reaper is None or not self._reaper.is_alive():
            self._stop.clear()
            self._reaper = threading.Thread(target=self._reap_loop, name="rizin-reaper", daemon=True)
            self._reaper.start()

    def _reap_loop(self) -> None:
        interval = max(0.2, min(30.0, self.idle_timeout / 4))
        while not self._stop.wait(interval):
            self.reap_idle()

    def reap_idle(self) -> int:
        now = time.monotonic()
        closed = 0
        for s in self.sessions():
            if s._pipe is None or now - s.last_used < self.idle_timeout:
                continue
            if s.lock.acquire(blocking=False):
                try:
                    if s._pipe is not None and time.monotonic() - s.last_used >= self.idle_timeout:
                        s._pipe.quit()
                        s._pipe = None
                        s._analyzed = None
                        closed += 1
                finally:
                    s.lock.release()
        return closed

    def kill_all(self) -> None:
        for s in self.sessions():
            s.kill()

    def close_all(self) -> None:
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.kill()
            try:
                s.close()
            except Exception:
                pass


# =============================================================================================== built-in sample
def builtin_sample_elf() -> bytes:
    """A 160-byte static x86-64 ELF: _start calls f, f calls g, exit(0). Used by smoke() (no external files)."""
    base = 0x400000
    code = bytes.fromhex(
        "e809000000"      # _start: call f
        "b83c000000"      # mov eax, 60
        "31ff"            # xor edi, edi
        "0f05"            # syscall
        "55"              # f: push rbp
        "4889e5"          # mov rbp, rsp
        "b82a000000"      # mov eax, 42
        "e802000000"      # call g
        "5d"              # pop rbp
        "c3"              # ret
        "b807000000"      # g: mov eax, 7
        "c3")             # ret
    entry = base + 64 + 56
    total = 64 + 56 + len(code)
    ehdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8 + struct.pack(
        "<HHIQQQIHHHHHH", 2, 0x3E, 1, entry, 64, 0, 0, 64, 56, 1, 64, 0, 0)
    phdr = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, total, total, 0x1000)
    return ehdr + phdr + code


# =============================================================================================== backend adapter
OPERATIONS = [
    Operation("info", "Binary info and headers (iIj, iHj)", {"case_id": "str", "module_id": "str"}, {"info": "object", "headers": "object"}),
    Operation("imports", "Imported symbols (iij)", {"case_id": "str", "module_id": "str"}, {"imports": "list"}),
    Operation("exports", "Exported symbols (iEj)", {"case_id": "str", "module_id": "str"}, {"exports": "list"}),
    Operation("sections", "Sections and segments (iSj, iSSj)", {"case_id": "str", "module_id": "str"}, {"sections": "list", "segments": "list"}),
    Operation("strings", "Strings in the whole binary (izzj), bounded count; untrusted", {"case_id": "str", "module_id": "str", "limit": "int"}, {"strings": "list"}),
    Operation("entrypoints", "Entry points (iej)", {"case_id": "str", "module_id": "str"}, {"entrypoints": "list"}),
    Operation("analyze", "Run analysis (aaa with timeout); cached by module sha + rizin version + settings", {"case_id": "str", "module_id": "str"}, {"analysis": "object"}),
    Operation("functions", "Analysed functions (aflj)", {"case_id": "str", "module_id": "str"}, {"functions": "list"}),
    Operation("xrefs", "Cross references to/from an address (axtj/axfj)", {"case_id": "str", "module_id": "str", "addr": "str"}, {"to": "list", "from": "list"}),
    Operation("callgraph", "Callees (agc json) and callers of a function", {"case_id": "str", "module_id": "str", "function": "str"}, {"callees": "list", "callers": "list"}),
    Operation("disasm", "Function disassembly (pdfj); untrusted", {"case_id": "str", "module_id": "str", "function": "str"}, {"disasm": "object"}),
    Operation("decompile", "Decompile a function: rz-ghidra pdgj if loaded, else labelled pseudo output; untrusted", {"case_id": "str", "module_id": "str", "function": "str"}, {"decompiled": "object"}),
    Operation("function_briefing", "Bounded packet: signature, size, callers/callees, strings/imports/constants, decompiled text", {"case_id": "str", "module_id": "str", "function": "str"}, {"briefing": "object"}),
    Operation("symbols", "Symbol table (isj); untrusted", {"case_id": "str", "module_id": "str"}, {"symbols": "list"}),
    Operation("function", "One function: signature, cc, blocks (afbj), variables (afvlj), annotations", {"case_id": "str", "module_id": "str", "function": "str"}, {"function": "object"}),
    Operation("disassemble", "Linear disassembly: count instructions or length bytes at an address", {"case_id": "str", "module_id": "str", "addr": "str", "count": "int", "length": "int"}, {"ops": "list"}),
    Operation("call_graph", "Bounded BFS call graph (callees/callers/both, depth 1..5)", {"case_id": "str", "module_id": "str", "function": "str", "depth": "int", "direction": "str"}, {"nodes": "list", "edges": "list"}),
    Operation("search_bytes", "Hex byte pattern search with ?? wildcards over the module file", {"case_id": "str", "module_id": "str", "pattern": "str"}, {"matches": "list"}),
    Operation("annotations", "Current persisted annotations (+ history)", {"case_id": "str", "module_id": "str"}, {"annotations": "object"}),
    Operation("rename", "Rename function/global/local; persisted annotation", {"case_id": "str", "module_id": "str", "kind": "str", "target": "str", "new_name": "str"}, {"change": "object"}),
    Operation("set_type", "Function prototype or local variable type; persisted annotation", {"case_id": "str", "module_id": "str", "kind": "str", "target": "str"}, {"change": "object"}),
    Operation("add_comment", "Comment at an address; persisted annotation", {"case_id": "str", "module_id": "str", "addr": "str", "text": "str"}, {"change": "object"}),
    Operation("apply_struct", "Declare C struct/union/enum/typedef; persisted annotation", {"case_id": "str", "module_id": "str", "declaration": "str"}, {"change": "object"}),
    Operation("patch_bytes", "Patch bytes in a COPY inside the work folder (never the original); needs confirm", {"case_id": "str", "module_id": "str", "addr": "str", "data_hex": "str", "confirm": "bool"}, {"copy_path": "str"}),
    Operation("relocations", "Relocations (irj)", {"case_id": "str", "module_id": "str"}, {"relocations": "list"}),
    Operation("packer_report", "Packer/entropy/overlay/section-anomaly report (static, no rizin)", {"case_id": "str", "module_id": "str"}, {"report": "object"}),
    Operation("unpack", "Unpack a UPX-packed module with the pinned upx -d into the case work folder (never in place); needs confirm", {"case_id": "str", "module_id": "str", "confirm": "bool"}, {"unpacked": "object"}),
    Operation("fetch_pdb", "Opt-in: download the module's PDB from a symbol server into the work folder; kept only if GUID+age match", {"case_id": "str", "module_id": "str", "confirm": "bool"}, {"path": "str"}),
    Operation("xref_index", "Build the SQLite cross-index functions<->strings<->xrefs (+ call edges) in the work folder", {"case_id": "str", "module_id": "str"}, {"counts": "object"}),
    Operation("search_index", "Search the cross-index: strings (with referencing functions) and function names", {"case_id": "str", "module_id": "str", "query": "str"}, {"strings": "list", "functions": "list"}),
    Operation("admin_raw", "Run a raw rizin command (admin/debug only)", {"case_id": "str", "module_id": "str", "cmd": "str"}, {"text": "str"}, dangerous=True),
]


class RizinBackend(BackendAdapter):
    backend_id = "rizin"

    def __init__(self, settings: Settings | None = None, *, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 analysis: dict[str, Any] | None = None, use_pdb: bool = True):
        self.settings = settings or get_settings()
        self.analysis = dict(analysis or DEFAULT_ANALYSIS)
        self.use_pdb = use_pdb   # a verified sidecar PDB (same GUID+age as the PE) is used automatically
        self.pool = SessionPool(self.settings.limits, idle_timeout=idle_timeout)
        self._tool: RizinTool | None = None

    # -- tools ----------------------------------------------------------------
    def tool(self, *, refresh: bool = False) -> RizinTool | None:
        if refresh or self._tool is None:
            self._tool = find_rizin(self.settings)
        return self._tool

    def _rizin_probe(self, tool: RizinTool | None) -> ToolProbe:
        common = dict(license=RIZIN_LICENSE, source=RIZIN_SOURCE, pinned=RIZIN_PINNED, integration="cli")
        if tool is None:
            searched = [str(p) for p in _plugin_candidates(Path(self.settings.tools_dir))] + ["PATH"]
            return ToolProbe("rizin", Availability.MISSING, detail="rizin not found; searched " + ", ".join(searched),
                             prerequisites=["rizin " + RIZIN_PINNED], **common)
        integrity = RIZIN_STATIC_SHA256 if tool.static else ""
        if tool.static:
            tarball = Path(self.settings.tools_dir) / f"rizin-{RIZIN_PINNED}-static-x86_64.tar.xz"
            if tarball.is_file():
                actual = _cached_sha256(tarball)
                if actual != RIZIN_STATIC_SHA256:
                    return ToolProbe("rizin", Availability.DETECTED, path=str(tool.exe), version=tool.version,
                                     detail=f"static tarball sha256 mismatch: {actual} != pinned {RIZIN_STATIC_SHA256}",
                                     integrity=actual, **common)
        detail = f"commit {tool.commit}; {'static release build' if tool.static else 'shared source build'}"
        if tool.manifest.get("rizin", {}).get("tag"):
            detail += f" ({tool.manifest['rizin']['tag']})"
        if tool.version and tool.version.lstrip("v") != RIZIN_PINNED.lstrip("v"):
            detail += f"; WARNING version {tool.version} differs from pinned {RIZIN_PINNED}"
        return ToolProbe("rizin", Availability.INSTALLED if tool.version else Availability.DETECTED, path=str(tool.exe),
                         version=tool.version, detail=detail if tool.version else tool.error, integrity=integrity, **common)

    def rz_ghidra_probe(self, tool: RizinTool | None = None) -> ToolProbe:
        tool = tool if tool is not None else self.tool()
        common = dict(license=RZ_GHIDRA_LICENSE, source=RZ_GHIDRA_SOURCE, pinned=RZ_GHIDRA_PINNED, integration="plugin",
                      prerequisites=["rizin shared build with headers (not the static release)", "cmake", "C++17 compiler"])
        if tool is None:
            return ToolProbe("rz-ghidra", Availability.MISSING, detail="rizin not found", **common)
        if not tool.ghidra_plugin:
            where = ", ".join(str(d) for d in (tool.lib_plugins, tool.user_plugins) if d)
            return ToolProbe("rz-ghidra", Availability.MISSING,
                             detail=f"no rz-ghidra plugin in {where or 'rizin plugin dirs'}; decompile falls back to labelled pseudo output",
                             **common)
        if tool.static:
            return ToolProbe("rz-ghidra", Availability.DETECTED, path=str(tool.ghidra_plugin),
                             detail="plugin file present but this rizin is statically linked and cannot load it", **common)
        built = tool.manifest.get("rz_ghidra", {}) if tool.manifest else {}
        version = (built.get("tag") or "").lstrip("v") or None
        detail = f"plugin for rizin {tool.version} at {tool.exe}"
        if built.get("commit"):
            detail += f"; built from {built.get('tag')} commit {built['commit']}"
        else:
            detail += "; build provenance unknown (no build manifest)"
        return ToolProbe("rz-ghidra", Availability.INSTALLED, path=str(tool.ghidra_plugin), version=version,
                         detail=detail, **common)

    def probe(self) -> BackendInfo:
        tool = self.tool(refresh=True)
        rz = self._rizin_probe(tool)
        gh = self.rz_ghidra_probe(tool)
        return BackendInfo(
            backend_id=self.backend_id, title="Rizin (rzpipe) native analysis",
            formats=["pe", "elf", "macho", "raw"], platforms=["linux", "windows", "macos"],
            profiles=["native_pe", "native_elf", "unity_il2cpp", "unreal", "gamemaker"],
            operations=list(OPERATIONS), tools=[rz],
            # rz-ghidra is optional: listed separately so its absence does not mark the whole backend missing.
            resources={"ram_mb": 512, "disk_mb": 50, "time": "seconds to minutes per module (aaa)",
                       "optional_tools": [gh.to_dict()], "decompiler": "rz-ghidra(pdg)" if gh.availability == Availability.INSTALLED
                       else "pdf+asm.pseudo(pseudo)"},
        )

    def smoke(self) -> ToolProbe:
        tool = self.tool()
        probe = self._rizin_probe(tool)
        if tool is None or not tool.version:
            return probe
        with tempfile.TemporaryDirectory(prefix="rz-smoke-") as d:
            sample = Path(d) / "smoke.elf"
            sample.write_bytes(builtin_sample_elf())
            sess = RizinSession(tool, sample, sha256=None, limits=self.settings.limits,
                                analysis={"command": "aaa", "analysis.timeout": 60})
            try:
                funcs, _ = sess.functions()
                n = len(funcs)
            except Exception as e:
                probe.detail = f"smoke failed: {type(e).__name__}: {e}"
                return probe
            finally:
                sess.close()
        if n >= 2:
            probe.availability = Availability.USABLE
            probe.detail += f"; smoke: aaa found {n} functions in built-in ELF sample"
        else:
            probe.detail += f"; smoke: aaa found only {n} functions (expected >= 2)"
        return probe

    def close(self) -> None:
        self.pool.close_all()

    # -- sessions ---------------------------------------------------------------
    def module_path(self, cases: Any, case_id: str, module_id: str) -> tuple[dict[str, Any], Path]:
        try:
            return native.module_file(cases, case_id, module_id)
        except ValueError as e:
            raise InvalidTarget(str(e)) from None

    def session_for(self, cases: Any, case_id: str, module_id: str) -> tuple[dict[str, Any], RizinSession]:
        tool = self.tool()
        if tool is None or not tool.version:
            raise RizinError("rizin is not available on this host (see doctor)")
        module, path = self.module_path(cases, case_id, module_id)
        copy = self.analysis_copy(cases, case_id, module_id, module)
        if copy is not None:   # consented unpack: analyse the unpacked copy in the work folder; the original stays as is
            path, module = copy["path"], {**module, "sha256": copy["sha256"], "original_sha256": module["sha256"]}
        sess = self.pool.get(tool, path, module["sha256"], self.analysis, scope=f"{case_id}:{module_id}")
        if sess.pdb is None and copy is None and self.use_pdb:
            sess.pdb = self._pdb_for(path, (module.get("meta") or {}).get("pdb"), self.work_dir(cases, case_id, module_id))
        if sess.annotator is None:
            work = self.work_dir(cases, case_id, module_id)
            sess.annotator = lambda s, poll: annotations.apply_all(s, annotations.load(cases, case_id, module_id)[1], work, poll)
        if not getattr(sess, "_sha_checked", False):
            actual = sha256_file(path)
            if copy is not None and actual != module["sha256"]:
                raise RizinError(f"unpacked copy of {module['rel_path']} changed on disk (sha256 {actual[:12]} != {module['sha256'][:12]})")
            if actual != module["sha256"]:
                raise RizinError(f"module {module['rel_path']} changed on disk since inventory (sha256 {actual[:12]} != {module['sha256'][:12]})")
            sess._sha_checked = True  # type: ignore[attr-defined]
        return module, sess

    def analysis_copy(self, cases: Any, case_id: str, module_id: str, module: dict[str, Any]) -> dict[str, Any] | None:
        """The consented unpacked copy recorded on the module (meta.analysis_copy), if it is inside this module's work
        folder and still exists. Anything else is ignored (the original is analysed)."""
        meta = (module.get("meta") or {}).get("analysis_copy")
        if not isinstance(meta, dict) or not meta.get("rel") or not meta.get("sha256"):
            return None
        from ..paths import is_within
        work = self.work_dir(cases, case_id, module_id).resolve()
        p = (work / meta["rel"]).resolve()
        if not is_within(p, work) or not p.is_file():
            return None
        return {"path": p, "sha256": meta["sha256"], "meta": meta}

    def work_dir(self, cases: Any, case_id: str, module_id: str) -> Path:
        """The session's private work folder (patched copies, generated type headers). Never the user's files."""
        root = cases.case_root(case_id) if hasattr(cases, "case_root") else Path(self.settings.data_dir) / "cases" / case_id
        return Path(root) / "re" / module_id

    def session_for_path(self, path: Path | str, sha256: str | None = None) -> RizinSession:
        """Direct session on a file (CLI/Cutter/tests). Same pooling and locking as case modules."""
        tool = self.tool()
        if tool is None or not tool.version:
            raise RizinError("rizin is not available on this host")
        p = resolve_final(path)
        sess = self.pool.get(tool, p, sha256 or sha256_file(p), self.analysis)
        if sess.pdb is None and self.use_pdb and sess._analyzed is None:
            sess.pdb = self._pdb_for(p, None, None)
        return sess

    @staticmethod
    def _pdb_for(path: Path, meta: dict[str, Any] | None, work: Path | None) -> dict[str, Any] | None:
        """A PDB whose GUID and age match the PE: a downloaded one recorded on the module (work folder), else a sidecar."""
        from . import pdb as pdbmod
        try:
            cv = pdbmod.codeview(path)
            if not cv:
                return None
            if isinstance(meta, dict) and meta.get("rel") and work is not None:
                from ..paths import is_within
                cand = (work / meta["rel"]).resolve()
                if is_within(cand, work.resolve()) and cand.is_file() and pdbmod.matches(cv, cand):
                    return {"path": str(cand), "key": cv["key"], "name": cv["pdb_name"], "source": "symbol server (work folder)"}
            side = pdbmod.find_sidecar(path, cv)
            if side is not None:
                return {"path": str(side), "key": cv["key"], "name": cv["pdb_name"], "source": "next to the program"}
        except (OSError, ValueError):
            return None
        return None

    def cancel_module(self, cases: Any, case_id: str, module_id: str) -> bool:
        """Kill the rizin process tree serving a module (the session is re-created on next use)."""
        try:
            module, path = self.module_path(cases, case_id, module_id)
        except (KeyError, InvalidTarget):
            return False
        for s in self.pool.sessions():
            if s.path == path:
                s.kill()
                return True
        return False

    # -- evidence plumbing ---------------------------------------------------------
    def _inputs(self, sess_tool: RizinTool, module: dict[str, Any], op: str, args: dict[str, Any], *,
                analysis: bool, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        inputs: dict[str, Any] = {"op": op, "args": args, "module_sha256": module["sha256"],
                                  "rizin_version": sess_tool.version, "rizin_commit": sess_tool.commit,
                                  "analysis": dict(self.analysis) if analysis else None}
        if extra:
            inputs.update(extra)
        return inputs

    def _op(self, ctx: Any, case_id: str, module_id: str, *, op: str, args: dict[str, Any], analysis: bool,
            untrusted: bool, title: str, body_fn: Callable[[RizinSession, Callable[[], None] | None], tuple[Any, bool, dict[str, Any]]],
            extra_inputs: Callable[[RizinSession], dict[str, Any]] | None = None, use_cache: bool = True) -> OperationResult:
        kind = f"native.{op}"
        try:
            cases = native.cases_of(ctx)
            poll = native.poll_of(ctx)
            module, sess = self.session_for(cases, case_id, module_id)
            extra = dict(extra_inputs(sess)) if extra_inputs else {}
            if analysis and sess.pdb:   # symbols from a verified PDB change analysis output: part of the cache key
                extra["pdb"] = sess.pdb["key"]
            if analysis:  # analysis-derived output changes when annotations change: they are part of the cache key
                extra["annotations_rev"] = annotations.load(cases, case_id, module_id)[0]
            inputs = self._inputs(sess.tool, module, op, args, analysis=analysis, extra=extra or None)
            if use_cache:
                hit = native.cached_evidence(cases, case_id, module_id, kind, inputs)
                if hit is not None and hit.get("blob_sha"):
                    body = cases.blobs.get_json(hit["blob_sha"])
                    return OperationResult(ok=True, data={**body, "cached": True}, evidence_ids=[hit["evidence_id"]],
                                           truncated=bool(hit["meta"].get("truncated")))
            body, truncated, meta = body_fn(sess, poll)
            ev = native.store_evidence(cases, case_id, module_id, kind, title, body, inputs, untrusted=untrusted,
                                       truncated=truncated, extra_meta={"rizin": sess.tool.version, **(meta or {})})
            return OperationResult(ok=True, data={**body, "cached": False}, evidence_ids=[ev["evidence_id"]], truncated=truncated)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")
        except RizinTimeout as e:
            return OperationResult(ok=False, error=f"timeout: {e}")
        except (RizinError, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    # -- typed operations ------------------------------------------------------------
    def op_info(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s: RizinSession, poll):
            info, t1 = s.info(poll)
            headers, t2 = s.headers(poll)
            return {"info": info, "headers": headers}, t1 or t2, {}
        return self._op(ctx, case_id, module_id, op="info", args={}, analysis=False, untrusted=True,
                        title="Binary info and headers", body_fn=body)

    def op_imports(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            data, t = s.imports(poll)
            return {"imports": data or [], "count": len(data or [])}, t, {}
        return self._op(ctx, case_id, module_id, op="imports", args={}, analysis=False, untrusted=True, title="Imports", body_fn=body)

    def op_exports(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            data, t = s.exports(poll)
            return {"exports": data or [], "count": len(data or [])}, t, {}
        return self._op(ctx, case_id, module_id, op="exports", args={}, analysis=False, untrusted=True, title="Exports", body_fn=body)

    def op_sections(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            secs, t1 = s.sections(poll)
            segs, t2 = s.segments(poll)
            return {"sections": secs or [], "segments": segs or []}, t1 or t2, {}
        return self._op(ctx, case_id, module_id, op="sections", args={}, analysis=False, untrusted=True,
                        title="Sections and segments", body_fn=body)

    def op_entrypoints(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            data, t = s.entrypoints(poll)
            return {"entrypoints": [{**e, "addr": native.hexaddr(e.get("vaddr"))} for e in data or []]}, t, {}
        return self._op(ctx, case_id, module_id, op="entrypoints", args={}, analysis=False, untrusted=False,
                        title="Entry points", body_fn=body)

    def op_strings(self, ctx: Any, *, case_id: str, module_id: str, limit: int = 2000) -> OperationResult:
        limit = max(1, min(int(limit), MAX_STRINGS))

        def body(s, poll):
            data, t = s.strings(poll)
            data = [x for x in data or [] if isinstance(x, dict)]
            items = []
            for x in data[:limit]:
                st = x.get("string") or ""
                items.append({"vaddr": x.get("vaddr"), "paddr": x.get("paddr"), "length": x.get("length"),
                              "section": x.get("section"), "type": x.get("type"), "string": native.clip(st, MAX_STRING_CHARS)})
            count_truncated = len(data) > limit
            return ({"untrusted": True, "note": native.UNTRUSTED_NOTE, "strings": items, "returned": len(items),
                     "seen": len(data), "limit": limit, "output_truncated": t, "count_truncated": count_truncated},
                    t or count_truncated, {})
        return self._op(ctx, case_id, module_id, op="strings", args={"limit": limit}, analysis=False, untrusted=True,
                        title=f"Strings (izzj, up to {limit})", body_fn=body)

    def op_analyze(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            report = s.analyze(poll)
            funcs = s.cached_functions(poll)
            return {"analysis": report, "functions_count": len(funcs)}, False, {"analysis": dict(self.analysis)}
        return self._op(ctx, case_id, module_id, op="analyze", args={}, analysis=True, untrusted=False,
                        title=f"Analysis ({self.analysis.get('command')})", body_fn=body)

    def op_functions(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            funcs, t = s.functions(poll)
            funcs = sorted(funcs, key=lambda f: f.get("offset", 0))
            over = len(funcs) > MAX_FUNCTIONS
            items = [native.summarize_function(f) for f in funcs[:MAX_FUNCTIONS]]
            return {"functions": items, "count": len(funcs), "returned": len(items)}, t or over, {}
        return self._op(ctx, case_id, module_id, op="functions", args={}, analysis=True, untrusted=True,
                        title="Functions (aflj)", body_fn=body)

    def op_xrefs(self, ctx: Any, *, case_id: str, module_id: str, addr: str | int) -> OperationResult:
        try:
            target = normalize_target(addr)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            a, _ = s.resolve(addr, need_function=False, poll=poll)
            to, t1 = s.xrefs_to(a, poll)
            frm, t2 = s.xrefs_from(a, poll)
            return {"addr": native.hexaddr(a), "to": to or [], "from": frm or []}, t1 or t2, {"address": native.hexaddr(a)}
        return self._op(ctx, case_id, module_id, op="xrefs", args={"target": target}, analysis=True, untrusted=False,
                        title=f"Xrefs {target}", body_fn=body)

    def op_callgraph(self, ctx: Any, *, case_id: str, module_id: str, function: str | int) -> OperationResult:
        try:
            target = normalize_target(function)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            a, fn = s.resolve(function, need_function=True, poll=poll)
            a = fn["offset"]
            graph, t1 = s.callgraph(a, poll)
            nodes = (graph or {}).get("nodes", []) if isinstance(graph, dict) else []
            callees: list[dict[str, Any]] = []
            seen: set[Any] = set()
            for n in nodes[1:]:
                if n.get("offset") in seen:
                    continue
                seen.add(n.get("offset"))
                callees.append({"name": n.get("title"), "addr": native.hexaddr(n.get("offset"))})
            xto, t2 = s.xrefs_to(a, poll)
            funcs = s.cached_functions(poll)
            callers = []
            seen_c: set[int] = set()
            for x in xto or []:
                if x.get("type") != "CALL":
                    continue
                owner = next((f for f in funcs if f.get("minbound", f.get("offset", 0)) <= x["from"] < f.get("maxbound", 0)), None)
                key = owner["offset"] if owner else x["from"]
                if key in seen_c:
                    continue
                seen_c.add(key)
                callers.append({"name": owner.get("name") if owner else None, "addr": native.hexaddr(key),
                                "call_site": native.hexaddr(x["from"])})
            return ({"function": fn.get("name"), "addr": native.hexaddr(a), "callees": callees, "callers": callers},
                    t1 or t2, {"address": native.hexaddr(a)})
        return self._op(ctx, case_id, module_id, op="callgraph", args={"target": target}, analysis=True, untrusted=True,
                        title=f"Call graph {target}", body_fn=body)

    def op_disasm(self, ctx: Any, *, case_id: str, module_id: str, function: str | int) -> OperationResult:
        try:
            target = normalize_target(function)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            a, fn = s.resolve(function, need_function=True, poll=poll)
            data, t = s.disasm(fn["offset"], poll)
            slim = native.slim_disasm(data)
            annotations.overlay_disasm(slim["ops"], annotations.load(native.cases_of(ctx), case_id, module_id)[1])
            return ({"untrusted": True, "note": native.UNTRUSTED_NOTE, "function": fn.get("name"),
                     "addr": native.hexaddr(fn["offset"]), "disasm": slim}, t, {"address": native.hexaddr(fn["offset"])})
        return self._op(ctx, case_id, module_id, op="disasm", args={"target": target}, analysis=True, untrusted=True,
                        title=f"Disassembly {target}", body_fn=body)

    def _expected_decompiler(self, sess: RizinSession) -> dict[str, Any]:
        t = sess.tool
        if t.ghidra_plugin and not t.static:
            built = t.manifest.get("rz_ghidra", {}) if t.manifest else {}
            return {"decompiler": "rz-ghidra(pdg)", "rz_ghidra": built.get("tag") or RZ_GHIDRA_PINNED,
                    "rz_ghidra_commit": built.get("commit"), "rz_ghidra_plugin": t.ghidra_plugin.name}
        return {"decompiler": "pseudo", "rz_ghidra": None}

    def op_decompile(self, ctx: Any, *, case_id: str, module_id: str, function: str | int) -> OperationResult:
        try:
            target = normalize_target(function)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            a, fn = s.resolve(function, need_function=True, poll=poll)
            dec = s.decompile(fn["offset"], poll)
            state = annotations.load(native.cases_of(ctx), case_id, module_id)[1]
            lo, hi = fn.get("minbound", fn["offset"]), fn.get("maxbound", fn["offset"] + (fn.get("size") or 0))
            text = annotations.overlay_decompiled(dec["text"], annotations.comments_in(state, lo, hi))
            env = native.untrusted_text(text, self.settings.limits.max_subprocess_output_bytes)
            return ({"function": fn.get("name"), "addr": native.hexaddr(fn["offset"]), "decompiler": dec["decompiler"],
                     "is_real_decompiler": dec["is_real_decompiler"], "decompiled": env},
                    bool(dec.get("truncated")),
                    {"decompiler": dec["decompiler"], "is_real_decompiler": dec["is_real_decompiler"],
                     "address": native.hexaddr(fn["offset"])})
        return self._op(ctx, case_id, module_id, op="decompile", args={"target": target}, analysis=True, untrusted=True,
                        title=f"Decompile {target}", body_fn=body, extra_inputs=self._expected_decompiler)

    def op_function_briefing(self, ctx: Any, *, case_id: str, module_id: str, function: str | int,
                             max_items: int = native.BRIEFING_MAX_ITEMS,
                             max_decompiled_bytes: int = native.BRIEFING_MAX_DECOMPILED_BYTES) -> OperationResult:
        try:
            target = normalize_target(function)
            cases = native.cases_of(ctx)
        except (InvalidTarget, ValueError) as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")
        max_items = max(1, min(int(max_items), 500))
        max_decompiled_bytes = max(256, min(int(max_decompiled_bytes), self.settings.limits.max_context_bytes))
        brief_args = {"target": target, "max_items": max_items, "max_decompiled_bytes": max_decompiled_bytes}
        try:
            module, sess = self.session_for(cases, case_id, module_id)
            inputs = self._inputs(sess.tool, module, "function_briefing", brief_args, analysis=True,
                                  extra={**self._expected_decompiler(sess),
                                         "annotations_rev": annotations.load(cases, case_id, module_id)[0]})
            hit = native.cached_evidence(cases, case_id, module_id, "native.briefing", inputs)
            if hit is not None and hit.get("blob_sha"):
                packet = cases.blobs.get_json(hit["blob_sha"])
                return OperationResult(ok=True, data={**packet, "cached": True}, evidence_ids=packet.get("evidence_ids", []),
                                       truncated=bool(packet.get("any_truncated")))
        except (RizinError, InvalidTarget, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")
        parts: dict[str, OperationResult] = {}
        for name, fn in (("functions", lambda: self.op_functions(ctx, case_id=case_id, module_id=module_id)),
                         ("imports", lambda: self.op_imports(ctx, case_id=case_id, module_id=module_id)),
                         ("strings", lambda: self.op_strings(ctx, case_id=case_id, module_id=module_id, limit=MAX_STRINGS)),
                         ("disasm", lambda: self.op_disasm(ctx, case_id=case_id, module_id=module_id, function=function)),
                         ("decompile", lambda: self.op_decompile(ctx, case_id=case_id, module_id=module_id, function=function))):
            r = fn()
            if not r.ok:
                return OperationResult(ok=False, error=f"{name}: {r.error}", evidence_ids=[i for p in parts.values() for i in p.evidence_ids])
            parts[name] = r
        try:
            poll = native.poll_of(ctx)
            addr, fninfo = sess.resolve(function, need_function=True, poll=poll)
            xr = self.op_xrefs(ctx, case_id=case_id, module_id=module_id, addr=fninfo["offset"])
            if not xr.ok:
                return OperationResult(ok=False, error=f"xrefs: {xr.error}")
            parts["xrefs"] = xr
            funcs = sess.cached_functions(poll)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")
        except (RizinError, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")
        ev_ids = [i for p in parts.values() for i in p.evidence_ids]
        packet = native.build_briefing(
            module=module, fn=fninfo, functions=funcs, imports=parts["imports"].data.get("imports", []),
            strings=parts["strings"].data.get("strings", []), xrefs_to=parts["xrefs"].data.get("to", []),
            disasm=parts["disasm"].data.get("disasm", {}),
            decompiled={"text": parts["decompile"].data["decompiled"]["text"],
                        "decompiler": parts["decompile"].data["decompiler"],
                        "is_real_decompiler": parts["decompile"].data["is_real_decompiler"]},
            evidence_ids=ev_ids, max_items=max_items, max_decompiled_bytes=max_decompiled_bytes)
        ev = native.store_evidence(cases, case_id, module_id, "native.briefing", f"Function briefing {packet['function']['name']}",
                                   packet, inputs, untrusted=True, truncated=packet["any_truncated"],
                                   extra_meta={"rizin": sess.tool.version, "decompiler": packet["decompiled"]["decompiler"],
                                               "address": packet["function"]["addr"]})
        packet["evidence_ids"] = [ev["evidence_id"]] + packet["evidence_ids"]
        return OperationResult(ok=True, data=packet, evidence_ids=packet["evidence_ids"], truncated=packet["any_truncated"])

    # -- reverse-engineering workbench operations (MCP `re` toolset; the R6 app workbench reuses these) ---------------
    def op_symbols(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            data, t = s.symbols(poll)
            items = [{k: x.get(k) for k in ("name", "flagname", "realname", "type", "bind", "vaddr", "paddr", "size", "is_imported")
                      if k in x} for x in data or [] if isinstance(x, dict)]
            for x in items:
                x["addr"] = native.hexaddr(x.get("vaddr")) if isinstance(x.get("vaddr"), int) else None
            return {"symbols": items, "count": len(items)}, t, {}
        return self._op(ctx, case_id, module_id, op="symbols", args={}, analysis=False, untrusted=True, title="Symbols", body_fn=body)

    def op_function(self, ctx: Any, *, case_id: str, module_id: str, function: str | int) -> OperationResult:
        """One function: signature, calling convention, size, basic blocks, variables and its annotations."""
        try:
            target = normalize_target(function)
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            _, fn = s.resolve(function, need_function=True, poll=poll)
            a = fn["offset"]
            blocks, t1 = s.blocks(a, poll)
            vars_, t2 = s.variables(a, poll)
            variables = []
            if isinstance(vars_, dict):
                for group, rows in vars_.items():
                    for v in rows if isinstance(rows, list) else []:
                        if isinstance(v, dict):
                            variables.append({"name": v.get("name"), "type": v.get("type"), "is_arg": bool(v.get("arg")),
                                              "storage": group, "location": v.get("storage")})
            state = annotations.load(native.cases_of(ctx), case_id, module_id)[1]
            key = native.hexaddr(a)
            lo, hi = fn.get("minbound", a), fn.get("maxbound", a + (fn.get("size") or 0))
            ann = {"function": state["functions"].get(key), "locals": state["locals"].get(key) or {},
                   "comments": [{"addr": native.hexaddr(x), "text": tx} for x, tx in annotations.comments_in(state, lo, hi)]}
            info = native.summarize_function(fn)
            info["callees"] = len({r.get("to") for r in fn.get("callrefs") or [] if r.get("type") == "CALL"})
            blk = [{"addr": native.hexaddr(b.get("addr")), "size": b.get("size"), "ninstr": b.get("ninstr"),
                    "jump": native.hexaddr(b.get("jump")) if b.get("jump") else None,
                    "fail": native.hexaddr(b.get("fail")) if b.get("fail") else None}
                   for b in blocks or [] if isinstance(b, dict)]
            return ({"function": info, "addr": key, "blocks": blk, "variables": variables, "annotations": ann},
                    t1 or t2, {"address": key})
        return self._op(ctx, case_id, module_id, op="function", args={"target": target}, analysis=True, untrusted=True,
                        title=f"Function {target}", body_fn=body)

    def op_disassemble(self, ctx: Any, *, case_id: str, module_id: str, addr: str | int, count: int | None = None,
                       length: int | None = None) -> OperationResult:
        """Linear disassembly of ``count`` instructions (<= 2000) or ``length`` bytes (<= 64 KiB) at a mapped address."""
        try:
            target = normalize_target(addr)
            if (count is None) == (length is None):
                raise InvalidTarget("give exactly one of count or length")
            if count is not None and not 1 <= int(count) <= 2000:
                raise InvalidTarget("count must be 1..2000")
            if length is not None and not 1 <= int(length) <= 65536:
                raise InvalidTarget("length must be 1..65536")
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            a, _ = s.resolve(addr, need_function=False, poll=poll)
            data, t = s.disasm_linear(a, count=count, length=length, poll=poll)
            ops = [{k: op[k] for k in native.DISASM_OP_FIELDS if k in op} for op in data or [] if isinstance(op, dict)]
            for op in ops:
                op["addr"] = native.hexaddr(op.get("offset"))
            annotations.overlay_disasm(ops, annotations.load(native.cases_of(ctx), case_id, module_id)[1])
            return ({"untrusted": True, "note": native.UNTRUSTED_NOTE, "addr": native.hexaddr(a), "ops": ops}, t,
                    {"address": native.hexaddr(a)})
        return self._op(ctx, case_id, module_id, op="disassemble", args={"target": target, "count": count, "length": length},
                        analysis=True, untrusted=True, title=f"Disassembly {target}", body_fn=body)

    def op_call_graph(self, ctx: Any, *, case_id: str, module_id: str, function: str | int, depth: int = 2,
                      direction: str = "callees", max_nodes: int = 200) -> OperationResult:
        """Bounded call graph (breadth-first) from one function: callees, callers or both, ``depth`` 1..5, <= 500 nodes."""
        try:
            target = normalize_target(function)
            if direction not in ("callees", "callers", "both"):
                raise InvalidTarget("direction must be callees, callers or both")
            depth = int(depth)
            max_nodes = int(max_nodes)
            if not 1 <= depth <= 5 or not 1 <= max_nodes <= 500:
                raise InvalidTarget("depth must be 1..5 and max_nodes 1..500")
        except (InvalidTarget, TypeError, ValueError) as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")

        def body(s, poll):
            _, fn = s.resolve(function, need_function=True, poll=poll)
            funcs = s.cached_functions(poll)
            by_off = {f["offset"]: f for f in funcs if isinstance(f.get("offset"), int)}
            ranges = sorted((f.get("minbound", f["offset"]), f.get("maxbound", f["offset"] + (f.get("size") or 0)), f["offset"])
                            for f in by_off.values())

            def owner(x: int) -> int | None:
                for lo, hi, off in ranges:
                    if lo <= x < hi:
                        return off
                return None
            root = fn["offset"]
            nodes: dict[int, dict[str, Any]] = {root: {"addr": native.hexaddr(root), "name": fn.get("name"), "depth": 0}}
            edges: set[tuple[int, int]] = set()
            frontier, truncated = [root], False
            for d in range(1, depth + 1):
                nxt: list[int] = []
                for cur in frontier:
                    links: list[tuple[int, int]] = []
                    if direction in ("callees", "both"):
                        f = by_off.get(cur) or {}
                        links += [(cur, r["to"]) for r in f.get("callrefs") or []
                                  if r.get("type") == "CALL" and isinstance(r.get("to"), int)]
                    if direction in ("callers", "both"):
                        xto, _ = s.xrefs_to(cur, poll)
                        for x in xto or []:
                            if x.get("type") == "CALL" and isinstance(x.get("from"), int):
                                o = owner(x["from"])
                                links.append((o if o is not None else x["from"], cur))
                    for a, b in links:
                        other = b if a == cur else a
                        if other not in nodes:
                            if len(nodes) >= max_nodes:
                                truncated = True
                                continue
                            f = by_off.get(other)
                            nodes[other] = {"addr": native.hexaddr(other), "name": f.get("name") if f else None, "depth": d}
                            nxt.append(other)
                        edges.add((a, b))
                frontier = nxt
            edge_list = [{"from": native.hexaddr(a), "to": native.hexaddr(b)} for a, b in sorted(edges) if a in nodes and b in nodes]
            return ({"root": native.hexaddr(root), "direction": direction, "depth": depth, "nodes": list(nodes.values()),
                     "edges": edge_list, "node_limit_hit": truncated}, truncated, {"address": native.hexaddr(root)})
        return self._op(ctx, case_id, module_id, op="call_graph",
                        args={"target": target, "depth": depth, "direction": direction, "max_nodes": max_nodes},
                        analysis=True, untrusted=True, title=f"Call graph {target} ({direction}, depth {depth})", body_fn=body)

    def op_search_bytes(self, ctx: Any, *, case_id: str, module_id: str, pattern: str, limit: int = 100) -> OperationResult:
        """Search the module file for a hex byte pattern ('48 8b ?? 24', '??' = any byte). Python-side, no rizin command."""
        try:
            needle = parse_byte_pattern(pattern)
            limit = max(1, min(int(limit), 1000))
        except (InvalidTarget, TypeError, ValueError) as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")
        canon = " ".join("??" if b is None else f"{b:02x}" for b in needle)

        def body(s, poll):
            secs, _ = s.sections(poll)
            regex = re.compile(b"".join(b"." if b is None else re.escape(bytes([b])) for b in needle), re.DOTALL)
            data = s.path.read_bytes()
            hits, more = [], False
            for m in regex.finditer(data):
                if len(hits) >= limit:
                    more = True
                    break
                off = m.start()
                sec = next((x for x in secs or [] if isinstance(x.get("paddr"), int) and x.get("size")
                            and x["paddr"] <= off < x["paddr"] + x["size"]), None)
                va = sec["vaddr"] + (off - sec["paddr"]) if sec and isinstance(sec.get("vaddr"), int) else None
                hits.append({"paddr": native.hexaddr(off), "addr": native.hexaddr(va), "section": sec.get("name") if sec else None})
            return {"pattern": canon, "matches": hits, "returned": len(hits), "limit": limit, "more": more}, more, {}
        return self._op(ctx, case_id, module_id, op="search_bytes", args={"pattern": canon, "limit": limit}, analysis=False,
                        untrusted=False, title=f"Byte search {canon[:60]}", body_fn=body)

    # ---- annotations (persisted as evidence revisions, re-applied after every re-analysis) -------------------------
    def op_annotations(self, ctx: Any, *, case_id: str, module_id: str, history: int = 0) -> OperationResult:
        try:
            cases = native.cases_of(ctx)
            rev, state, ev = annotations.load(cases, case_id, module_id)
            data: dict[str, Any] = {"revision": rev, "annotations": state,
                                    "label": "annotations are user/model proposals, not analysis facts"}
            if history:
                data["history"] = annotations.history(cases, case_id, module_id, limit=max(1, min(int(history), 200)))
            return OperationResult(ok=True, data=data, evidence_ids=[ev] if ev else [])
        except (KeyError, ValueError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def _mutate(self, ctx: Any, case_id: str, module_id: str, author: str,
                fn: Callable[..., tuple[dict[str, Any], dict[str, Any]]]) -> OperationResult:
        try:
            cases = native.cases_of(ctx)
            poll = native.poll_of(ctx)
            _, sess = self.session_for(cases, case_id, module_id)
            with sess.owned():
                sess.analyze(poll)   # also replays existing annotations onto a fresh process
                _, state, parent = annotations.load(cases, case_id, module_id)
                new_state, change = fn(sess, annotations.mutated(state), poll, self.work_dir(cases, case_id, module_id))
                ev = annotations.save(cases, case_id, module_id, new_state, change, parent=parent, author=author)
            return OperationResult(ok=True, data={"change": change, "revision": ev["revision"], "applied_to_analysis": True,
                                                  "note": "Persisted; re-applied automatically after re-analysis and on re-decompile."},
                                   evidence_ids=[ev["evidence_id"]])
        except (InvalidTarget, annotations.AnnotationError) as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")
        except RizinTimeout as e:
            return OperationResult(ok=False, error=f"timeout: {e}")
        except (RizinError, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def op_rename(self, ctx: Any, *, case_id: str, module_id: str, kind: str, target: str | int, new_name: str,
                  function: str | int | None = None, author: str = "user") -> OperationResult:
        """Rename a function (target = its address/name), a global (target = address; sets a flag) or a local variable
        (function = its function, target = the variable's current name)."""
        if kind not in ("function", "global", "local"):
            return OperationResult(ok=False, error="invalid target: kind must be function, global or local")

        def apply(s: RizinSession, state, poll, work):
            name = annotations.check_ident(new_name)
            if kind == "function":
                _, fn = s.resolve(target, need_function=True, poll=poll)
                a = fn["offset"]
                old = fn.get("name")
                s.rename_function(a, name, poll)
                got = (s.function_info(a, poll) or {}).get("name")
                if got != name:
                    raise InvalidTarget(f"rizin did not accept the name (function is still {got!r})")
                state["functions"].setdefault(native.hexaddr(a), {})["name"] = name
                return state, {"action": "rename", "kind": kind, "addr": native.hexaddr(a), "old": old, "new": name}
            if kind == "global":
                a = parse_address(target)
                if a is None:
                    raise InvalidTarget("global renames take an address (0x...)")
                s.validate_address(a, poll)
                key = native.hexaddr(a)
                prev = (state["globals"].get(key) or {}).get("name")
                if prev and prev != name:
                    s.remove_flag(prev, poll)
                s.set_flag(a, name, poll)
                got = s.flag_at(a, poll)
                if not got or got.get("name") != name:
                    raise InvalidTarget("rizin did not accept the global name")
                state["globals"][key] = {"name": name}
                return state, {"action": "rename", "kind": kind, "addr": key, "old": prev, "new": name}
            if function is None:
                raise InvalidTarget("local renames need the function (address or name) that owns the variable")
            cur = annotations.check_ident(str(target))
            _, fn = s.resolve(function, need_function=True, poll=poll)
            fa = fn["offset"]
            if cur not in _var_names(s, fa, poll):
                raise InvalidTarget(f"no variable {cur!r} in {fn.get('name')} "
                                    f"(variables: {', '.join(sorted(n for n in _var_names(s, fa, poll) if n))[:300]})")
            s.rename_variable(fa, cur, name, poll)
            if name not in _var_names(s, fa, poll):
                raise InvalidTarget(f"rizin did not accept the new name for {cur!r}")
            fkey = native.hexaddr(fa)
            orig = annotations.local_key(state, fkey, cur)
            state["locals"].setdefault(fkey, {}).setdefault(orig, {})["name"] = name
            return state, {"action": "rename", "kind": kind, "function": fkey, "old": cur, "new": name, "original": orig}
        return self._mutate(ctx, case_id, module_id, author, apply)

    def op_set_type(self, ctx: Any, *, case_id: str, module_id: str, kind: str, target: str | int,
                    prototype: str | None = None, variable: str | None = None, ctype: str | None = None,
                    author: str = "user") -> OperationResult:
        """kind=function: apply a C prototype (its name becomes the function's name). kind=local: set a variable's type."""
        if kind not in ("function", "local"):
            return OperationResult(ok=False, error="invalid target: kind must be function or local")

        def apply(s: RizinSession, state, poll, work):
            _, fn = s.resolve(target, need_function=True, poll=poll)
            fa = fn["offset"]
            fkey = native.hexaddr(fa)
            if kind == "function":
                if prototype is None:
                    raise InvalidTarget("prototype is required for kind=function")
                proto, pname = annotations.check_prototype(prototype)
                s.set_prototype(fa, proto, poll)
                info = s.function_info(fa, poll) or {}
                if f"{pname}(" not in (info.get("signature") or "").replace(" (", "("):
                    raise InvalidTarget(f"rizin did not accept the prototype (signature is {info.get('signature')!r})")
                ent = state["functions"].setdefault(fkey, {})
                ent.update({"prototype": proto, "name": pname})
                return state, {"action": "set_type", "kind": kind, "addr": fkey, "prototype": proto,
                               "signature": info.get("signature")}
            if variable is None or ctype is None:
                raise InvalidTarget("variable and type are required for kind=local")
            var = annotations.check_ident(variable)
            t = annotations.check_ctype(ctype)
            if var not in _var_names(s, fa, poll):
                raise InvalidTarget(f"no variable {var!r} in {fn.get('name')}")
            s.retype_variable(fa, var, t, poll)
            got = _var_types(s, fa, poll).get(var)
            if got is None or " ".join(got.split()) != t:
                raise InvalidTarget(f"rizin did not accept type {t!r} for {var!r} (now {got!r}); "
                                    "declare structs first with apply_struct")
            orig = annotations.local_key(state, fkey, var)
            ent = state["locals"].setdefault(fkey, {}).setdefault(orig, {})
            ent["type"] = t
            if orig != var:
                ent.setdefault("name", var)
            return state, {"action": "set_type", "kind": kind, "function": fkey, "variable": var, "type": t}
        return self._mutate(ctx, case_id, module_id, author, apply)

    def op_add_comment(self, ctx: Any, *, case_id: str, module_id: str, addr: str | int, text: str,
                       author: str = "user") -> OperationResult:
        """Attach a comment to an address (empty text removes it). Shown in disassembly and decompiler output."""
        def apply(s: RizinSession, state, poll, work):
            a = parse_address(addr)
            if a is None:
                raise InvalidTarget("comments take an address (0x...)")
            s.validate_address(a, poll)
            body = annotations.check_comment(text)
            key = native.hexaddr(a)
            if body:
                state["comments"][key] = {"text": body}
            else:
                state["comments"].pop(key, None)
            return state, {"action": "comment", "addr": key, "text": body}
        return self._mutate(ctx, case_id, module_id, author, apply)

    def op_apply_struct(self, ctx: Any, *, case_id: str, module_id: str, declaration: str, author: str = "user") -> OperationResult:
        """Declare C types (struct/union/enum/typedef) for this module; usable afterwards in set_type."""
        def apply(s: RizinSession, state, poll, work):
            decl, names = annotations.check_declaration(declaration)
            s.load_types_file(annotations.type_file(work, decl), poll)
            for n in names:
                if n["kind"] == "typedef":
                    continue
                listing, _ = s.types(n["kind"], poll)
                if not any(isinstance(x, dict) and x.get("name") == n["name"] for x in listing or []):
                    raise InvalidTarget(f"rizin did not accept the declaration ({n['kind']} {n['name']} not defined afterwards)")
            sha = hashlib.sha256(decl.encode("utf-8")).hexdigest()
            state["types"] = [t for t in state["types"] if t.get("sha256") != sha]
            if len(state["types"]) >= annotations.MAX_TYPES:
                raise InvalidTarget(f"at most {annotations.MAX_TYPES} declarations per module")
            state["types"].append({"decl": decl, "names": names, "sha256": sha})
            return state, {"action": "apply_struct", "names": names, "sha256": sha}
        return self._mutate(ctx, case_id, module_id, author, apply)

    def op_patch_bytes(self, ctx: Any, *, case_id: str, module_id: str, addr: str | int, data_hex: str, confirm: bool = False,
                       author: str = "user") -> OperationResult:
        """Write bytes into a COPY of the module inside the session work folder. The user's original file is never opened
        for writing. ``confirm`` must be True. Analysis keeps using the original; open the copy to analyse the patch."""
        if confirm is not True:
            return OperationResult(ok=False, error="refused: patch_bytes writes a patched copy; pass confirm=true to proceed")
        try:
            new = bytes.fromhex(re.sub(r"\s+", "", data_hex or ""))
        except ValueError:
            return OperationResult(ok=False, error="invalid target: data must be hex bytes, e.g. '90 90'")
        if not 1 <= len(new) <= 4096:
            return OperationResult(ok=False, error="invalid target: 1..4096 bytes per patch")
        try:
            from ..paths import is_within
            cases = native.cases_of(ctx)
            poll = native.poll_of(ctx)
            module, sess = self.session_for(cases, case_id, module_id)
            a = parse_address(addr)
            if a is None:
                raise InvalidTarget("patches take an address (0x...)")
            secs, _ = sess.sections(poll)
            sec = next((x for x in secs or [] if isinstance(x.get("vaddr"), int) and isinstance(x.get("paddr"), int) and x.get("size")
                        and x["vaddr"] <= a and a + len(new) <= x["vaddr"] + x["size"]), None)
            if sec is None:
                raise InvalidTarget(f"0x{a:x}..+{len(new)} is not inside the file-backed bytes of one section")
            paddr = sec["paddr"] + (a - sec["vaddr"])
            work = self.work_dir(cases, case_id, module_id)
            copy_dir = work / "patched"
            copy_dir.mkdir(parents=True, exist_ok=True)
            original = resolve_final(sess.path)
            dest = copy_dir / Path(module["rel_path"]).name
            work_r = resolve_final(work)
            if resolve_final(dest) == original or not is_within(resolve_final(dest), work_r) or is_within(original, work_r):
                raise InvalidTarget("refusing: the patch destination would not be a separate copy inside the work folder")
            if not dest.is_file():
                shutil.copyfile(original, dest)
            with open(original, "rb") as f:
                f.seek(paddr)
                orig_bytes = f.read(len(new))
            with open(dest, "r+b") as f:
                f.seek(paddr)
                before = f.read(len(new))
                f.seek(paddr)
                f.write(new)
            if sha256_file(original) != module["sha256"]:   # belt and braces: the original must be untouched
                raise RizinError("original module changed on disk during patching; only the copy was written")
            body = {"addr": native.hexaddr(a), "paddr": native.hexaddr(paddr), "section": sec.get("name"), "length": len(new),
                    "original_bytes": orig_bytes.hex(), "previous_bytes": before.hex(), "new_bytes": new.hex(),
                    "copy_path": str(dest), "copy_sha256": sha256_file(dest), "original_sha256": module["sha256"],
                    "original_untouched": True, "author": author}
            ev = native.store_evidence(cases, case_id, module_id, "re.patch", f"Patch {len(new)} bytes at 0x{a:x} (copy)", body,
                                       {"op": "patch_bytes", "addr": body["addr"], "new": body["new_bytes"],
                                        "previous": body["previous_bytes"], "copy_sha256": body["copy_sha256"]},
                                       untrusted=False, producer=f"patch:{author}")
            return OperationResult(ok=True, data=body, evidence_ids=[ev["evidence_id"]])
        except InvalidTarget as e:
            return OperationResult(ok=False, error=f"invalid target: {e}")
        except (RizinError, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    # -- dangerous ----------------------------------------------------------------
    def op_relocations(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        def body(s, poll):
            data, t = s.relocations(poll)
            return {"relocations": data or [], "count": len(data or [])}, t, {}
        return self._op(ctx, case_id, module_id, op="relocations", args={}, analysis=False, untrusted=True,
                        title="Relocations", body_fn=body)

    def op_packer_report(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        """Static packer check of the ORIGINAL module file (never the unpacked copy)."""
        from . import packer
        try:
            cases = native.cases_of(ctx)
            module, path = self.module_path(cases, case_id, module_id)
            rep = packer.packer_report(path)
            copy = self.analysis_copy(cases, case_id, module_id, module)
            body = {"report": rep, "analysis_copy": (copy or {}).get("meta")}
            inputs = {"op": "packer_report", "module_sha256": module["sha256"], "copy": (copy or {}).get("sha256")}
            hit = native.cached_evidence(cases, case_id, module_id, "native.packer_report", inputs)
            if hit is not None:
                return OperationResult(ok=True, data={**body, "cached": True}, evidence_ids=[hit["evidence_id"]])
            ev = native.store_evidence(cases, case_id, module_id, "native.packer_report", f"Packer check: {rep['summary']}",
                                       body, inputs, untrusted=False, producer="rebuild-studio")
            return OperationResult(ok=True, data={**body, "cached": False}, evidence_ids=[ev["evidence_id"]])
        except (InvalidTarget, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def op_unpack(self, ctx: Any, *, case_id: str, module_id: str, confirm: bool = False) -> OperationResult:
        """Consented unpack (UPX only, pinned upx -d) into <case>/re/<module>/unpacked/. Later analysis of this module uses
        the unpacked copy; the original file is never written (its sha256 is re-checked)."""
        from . import packer
        if not confirm:
            return OperationResult(ok=False, error="unpacking runs the pinned upx -d on a copy in the case work folder; "
                                                   "pass confirm=true to allow it")
        try:
            cases = native.cases_of(ctx)
            module, path = self.module_path(cases, case_id, module_id)
            work = self.work_dir(cases, case_id, module_id)
            _p, info = packer.prepare_for_analysis(path, work / "unpacked", allow_unpack=True, tools_dir=self.settings.tools_dir)
            res = info.get("unpack") or {}
            if not info["report"]["packed"]:
                return OperationResult(ok=False, error="not packed: " + info["report"]["summary"])
            if not res.get("ok"):
                return OperationResult(ok=False, error=res.get("reason") or "unpack failed")
            rel = Path(res["unpacked_path"]).resolve().relative_to(work.resolve()).as_posix()
            copy = {"rel": rel, "sha256": res["unpacked_sha256"], "packer": info["report"]["packer"],
                    "tool": f"upx {res.get('tool_version')}", "tool_sha256": res.get("tool_sha256"),
                    "original_sha256": res["original_sha256"], "consent": "confirm=true"}
            meta = dict(module.get("meta") or {})
            meta["analysis_copy"] = copy
            cases.add_module(case_id, module["rel_path"], module["sha256"], module["size"], module["format"], module["profile"],
                             module.get("arch"), meta=meta)
            for s in self.pool.sessions():   # sessions on the packed file are retired; the next call opens the copy
                if s.path == path:
                    s.kill()
            body = {"unpacked": copy, "packer_report": info["report"], "unpacked_report": res.get("unpacked_report")}
            ev = native.store_evidence(cases, case_id, module_id, "native.unpack", f"Unpacked {module['rel_path']} ({copy['packer']})",
                                       body, {"op": "unpack", "module_sha256": module["sha256"], "tool_sha256": res.get("tool_sha256")},
                                       untrusted=False, producer="upx")
            return OperationResult(ok=True, data=body, evidence_ids=[ev["evidence_id"]])
        except (InvalidTarget, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def op_fetch_pdb(self, ctx: Any, *, case_id: str, module_id: str, confirm: bool = False,
                     server: str | None = None) -> OperationResult:
        """Opt-in: download this module's PDB from a symbol server (default Microsoft's) into the work folder. Allowed when
        the case setting ``symbol_server`` is true or ``confirm=true``; kept only if its GUID and age match the PE."""
        from . import pdb as pdbmod
        try:
            cases = native.cases_of(ctx)
            module, path = self.module_path(cases, case_id, module_id)
            allowed = bool(confirm) or bool((cases.get_case(case_id).get("settings") or {}).get("symbol_server"))
            if not allowed:
                return OperationResult(ok=False, error="symbol-server download is off for this project (setting symbol_server) "
                                                       "; pass confirm=true to allow it once")
            cv = pdbmod.codeview(path)
            if not cv:
                return OperationResult(ok=False, error="the program names no PDB (no CodeView RSDS record)")
            work = self.work_dir(cases, case_id, module_id)
            res = pdbmod.download(cv, work / "pdb", server=server or pdbmod.DEFAULT_SERVER)
            body = {"codeview": cv, **res}
            if res.get("ok"):
                meta = dict(module.get("meta") or {})
                meta["pdb"] = {"rel": Path(res["path"]).resolve().relative_to(work.resolve()).as_posix(), "key": cv["key"]}
                cases.add_module(case_id, module["rel_path"], module["sha256"], module["size"], module["format"], module["profile"],
                                 module.get("arch"), meta=meta)
                for s in self.pool.sessions():
                    if s.path == path:
                        s.kill()
                        s.pdb = None
            ev = native.store_evidence(cases, case_id, module_id, "native.pdb", f"PDB {cv['pdb_name']}: {'verified' if res.get('ok') else 'not found'}",
                                       body, {"op": "fetch_pdb", "module_sha256": module["sha256"], "key": cv["key"]},
                                       untrusted=False, producer="symbol-server")
            return OperationResult(ok=bool(res.get("ok")), data=body, error=None if res.get("ok") else res.get("reason"),
                                   evidence_ids=[ev["evidence_id"]])
        except (InvalidTarget, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def xref_index_path(self, cases: Any, case_id: str, module_id: str) -> Path:
        return self.work_dir(cases, case_id, module_id) / "xref_index.sqlite"

    def op_xref_index(self, ctx: Any, *, case_id: str, module_id: str) -> OperationResult:
        from . import xref_index

        def body(s: RizinSession, poll):
            cases = native.cases_of(ctx)
            funcs = s.cached_functions(poll)
            strs, t1 = s.strings(poll)
            xr, t2 = s.all_xrefs(poll)
            db = self.xref_index_path(cases, case_id, module_id)
            from . import rizin_passes
            ptrs = rizin_passes.reloc_pointers(s.path)
            info, _ = s.info(poll)
            delta = (int((info or {}).get("baddr") or 0) - ptrs["image_base"]) if "image_base" in ptrs and (info or {}).get("baddr") else 0
            counts = xref_index.build(db, functions=funcs, strings=[x for x in strs or [] if isinstance(x, dict)],
                                      xrefs=[x for x in xr or [] if isinstance(x, dict)],
                                      pointers=[(a + delta, v + delta) for a, v in ptrs["pairs"]],
                                      meta={"module_sha256": s.sha256, "rizin": s.tool.version, "analysis": json.dumps(self.analysis)})
            return {"counts": counts, "path": str(db)}, t1 or t2, {}
        return self._op(ctx, case_id, module_id, op="xref_index", args={}, analysis=True, untrusted=False,
                        title="Cross-index (functions, strings, xrefs)", body_fn=body, use_cache=False)

    def op_search_index(self, ctx: Any, *, case_id: str, module_id: str, query: str, limit: int = 50) -> OperationResult:
        from . import xref_index
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return OperationResult(ok=False, error="query must be 1..200 characters")
        try:
            cases = native.cases_of(ctx)
            db = self.xref_index_path(cases, case_id, module_id)
            if not db.is_file():
                built = self.op_xref_index(ctx, case_id=case_id, module_id=module_id)
                if not built.ok:
                    return built
            res = xref_index.search(db, query, limit=limit)
            return OperationResult(ok=True, data={"untrusted": True, "note": native.UNTRUSTED_NOTE, **res}, truncated=res["truncated"])
        except (InvalidTarget, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")

    def admin_raw(self, ctx: Any, *, case_id: str, module_id: str, cmd: str, dangerous_ok: bool = False) -> OperationResult:
        """DANGEROUS raw command for human admins. Not an ``op_*`` method, so ``call()``/MCP/model routes cannot reach it."""
        if not dangerous_ok:
            return OperationResult(ok=False, error="admin_raw is dangerous; pass dangerous_ok=True from an admin-only surface")
        try:
            cases = native.cases_of(ctx)
            _, sess = self.session_for(cases, case_id, module_id)
            text, trunc = sess.admin_raw(cmd)
            pipe = sess._pipe
            return OperationResult(ok=True, data={"text": text, "dangerous": True,
                                                  "stderr_tail": pipe.stderr_tail(1000) if pipe else ""}, truncated=trunc)
        except (RizinError, InvalidTarget, KeyError, ValueError, OSError) as e:
            return OperationResult(ok=False, error=f"{type(e).__name__}: {e}")


def _var_rows(s: RizinSession, fa: int, poll) -> list[dict[str, Any]]:
    data, _ = s.variables(fa, poll)
    if not isinstance(data, dict):
        return []
    return [v for g in data.values() if isinstance(g, list) for v in g if isinstance(v, dict)]


def _var_names(s: RizinSession, fa: int, poll) -> set[str]:
    return {v.get("name") for v in _var_rows(s, fa, poll)}


def _var_types(s: RizinSession, fa: int, poll) -> dict[str, str]:
    return {v.get("name"): v.get("type") or "" for v in _var_rows(s, fa, poll)}


def parse_byte_pattern(pattern: str) -> list[int | None]:
    """'48 8b ?? 24' / '488b??24' -> [0x48, 0x8b, None, 0x24]. 1..256 bytes, at least one fixed byte."""
    if not isinstance(pattern, str):
        raise InvalidTarget("pattern must be a string")
    p = re.sub(r"\s+", "", pattern)
    if not p or len(p) % 2 or not re.fullmatch(r"(?:[0-9a-fA-F]{2}|\?\?)+", p):
        raise InvalidTarget("pattern must be hex byte pairs with optional '??' wildcards, e.g. '48 8b ?? 24'")
    out = [None if p[i:i + 2] == "??" else int(p[i:i + 2], 16) for i in range(0, len(p), 2)]
    if len(out) > 256 or all(b is None for b in out):
        raise InvalidTarget("pattern must be 1..256 bytes with at least one fixed byte")
    return out
