"""Guided, hash-verified installation of the external analysis tools (rizin, GDRE, ILSpy, .NET runtime, Node).

An ordinary Windows user never needs PowerShell/pip/npm/dotnet: the UI calls ``ToolSetup`` through ``api/tools_routes.py``.
The pinned lock (docs/dependency-lock.json) is the single source of truth. Rules enforced here:

- an artifact whose sha256 is ``null`` or ``verify_required`` is never installed (status ``blocked_unverified``);
- download over HTTPS only (loopback http is accepted only when the manager is built with ``allow_insecure_loopback``,
  which the tests use); redirects are followed manually and must stay on https;
- size, sha256 (and ``sha512_official`` when present) are checked BEFORE anything is extracted;
- extraction goes to a staging directory with zip-slip / symlink / expansion guards, ``layout.archive_root`` is honoured,
  ``layout.entry_sha256`` and ``layout.extra_files`` hashes are checked, then the staging directory is renamed into
  ``<tools_dir>/<install_dir>`` and a ``.rebuild-tool.json`` marker is written;
- downloaded content is executed in exactly two places, both on the STAGED copy before anything is activated, both with
  ``shell=False``, a timeout and no stdin: (1) the version check (entry + version_args); (2) an optional, lock-declared
  ``layout.post_install`` step (argv template with ``{staged}``/``{tools}`` placeholders, env, timeout, ``produces``). The
  post-install program must be a file inside the staged directory (the hash-verified entry), it is cancellable, its output
  goes to ``<tools>/.logs/<name>-install.log``, and any failure/cancel/timeout discards the staging directory so nothing is
  activated. The only user of post_install is the Rust toolchain: the pinned, sha256-verified official ``rustup-init.exe``
  downloads the pinned toolchain itself over HTTPS; rustup verifies every component against the hashes in Rust's signed
  release manifests (that second verification is rustup's, not ours). Nothing else is ever run from a download.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
import uuid
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings, get_settings
from .ids import now_iso
from .paths import PathPolicyError, safe_archive_target

log = logging.getLogger(__name__)

MARKER = ".rebuild-tool.json"
CHUNK = 256 * 1024
_HEX64 = set("0123456789abcdefABCDEF")

# Plain-language descriptions shown in the UI (the lock's own "role" text is developer-facing).
FRIENDLY = {
    "rizin": ("Rizin", "Needed to analyze native Windows programs (.exe and .dll files)."),
    "gdre": ("GDRE Tools", "Needed to recover Godot games (.pck files)."),
    "ilspycmd": ("ILSpy", "Needed to recover .NET programs (C# source from .exe and .dll files)."),
    "dotnet-runtime": ("Private .NET runtime", "Runs the .NET recovery tool. Installed privately inside Rebuild Studio, not on your PC."),
    "node": ("Node.js (optional)", "Optional. Unpacks Electron and JavaScript apps and runs browser behaviour tests."),
    "playwright-core": ("Browser test library (optional)", "Optional. Lets Rebuild Studio compare web apps in a browser (uses Microsoft Edge, already on Windows 11). Needs Node.js."),
    "temurin-jre": ("Private Java runtime (optional)", "Optional. Runs the Java and Android recovery tools. Installed privately inside Rebuild Studio, not on your PC."),
    "rust": ("Rust compiler (private)", "Optional. Needed to build rebuilt programs as Windows .exe files (Rust remakes). Installed privately inside Rebuild Studio (no administrator rights, no Visual Studio). Large: about 150 MB to download and 850 MB of disk."),
    "cfr": ("CFR (optional)", "Optional. Needed to recover Java programs (.jar files)."),
    "jadx": ("jadx (optional)", "Optional. Needed to recover Android apps (.apk files)."),
    "temurin-jdk21": ("Private Java 21 (optional)", "Optional. Runs the optional Ghidra decompiler. Installed privately inside Rebuild Studio, not on your PC. About 200 MB."),
    "ghidra": ("Ghidra (optional)", "Optional. A second decompiler for native programs, next to the built-in one. Large: about 570 MB to download. Needs Private Java 21."),
    "upx": ("UPX (optional)", "Optional. Unpacks programs compressed with UPX so they can be analyzed. Runs only when you allow it, on a copy (your file is never changed)."),
}


class ToolSetupError(Exception):
    """Carries the {code, message, affected, next_action} shape the API returns, plus a retryable flag."""

    def __init__(self, code: str, message: str, *, affected: str | None = None, next_action: str | None = None,
                 retryable: bool = False, status: int = 400, url: str | None = None):
        super().__init__(message)
        self.code, self.message, self.affected, self.next_action = code, message, affected, next_action
        self.retryable, self.status, self.url = retryable, status, url

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "affected": self.affected, "next_action": self.next_action,
                "retryable": self.retryable, "url": self.url}


class _Cancelled(Exception):
    pass


def find_lock_path() -> Path | None:
    """REBUILD_STUDIO_INSTALL/dependency-lock.json (shipped as a Tauri resource), repo docs/, then the packaged copy."""
    cands: list[Path] = []
    inst = os.environ.get("REBUILD_STUDIO_INSTALL")
    if inst:
        cands.append(Path(inst) / "dependency-lock.json")
    here = Path(__file__).resolve()
    cands.append(here.parents[2] / "docs" / "dependency-lock.json")
    cands.append(here.parent / "data" / "dependency-lock.json")
    for c in cands:
        if c.is_file():
            return c
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _is_hex64(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 64 and set(s) <= _HEX64


def _rmtree(path: Path) -> None:
    """Best-effort delete that also removes read-only files (rustup/cargo leave some)."""
    def _chmod_retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_chmod_retry)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=20, shell=False)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _part_meta(part: Path) -> Path:
    """Sidecar naming the pinned artifact a partial download belongs to (only such a .part is ever resumed)."""
    return part.with_name(part.name + ".json")


def _drop_part(part: Path) -> None:
    for p in (part, _part_meta(part)):
        try:
            p.unlink()
        except OSError:
            pass


def _fmt_mb(n: int | None) -> str:
    return f"{(n or 0) / 1_000_000:.0f} MB"


@dataclass
class _Job:
    chain: list[str]
    cancel: threading.Event
    phase: str = "queued"
    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""
    error: dict[str, Any] | None = None
    finished: bool = False
    last_emit: float = 0.0


class ToolSetup:
    def __init__(self, settings: Settings | None = None, events: Any = None, lock_path: Path | str | None = None,
                 *, allow_insecure_loopback: bool = False, tools_dir: Path | str | None = None):
        self.settings = settings or get_settings()
        self.events = events
        self.tools_dir = Path(tools_dir) if tools_dir else Path(self.settings.tools_dir)
        self.lock_path = Path(lock_path) if lock_path else find_lock_path()
        self.allow_insecure_loopback = allow_insecure_loopback
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._thread: threading.Thread | None = None
        self._verified: dict[tuple[str, int, int], bool] = {}
        self._lock_doc: dict[str, Any] | None = None
        self._hashes: dict[str, tuple[str, str | None]] = {}

    # ------------------------------------------------------------------------------------------------ lock access
    def _tools(self) -> dict[str, dict[str, Any]]:
        if self._lock_doc is None:
            if not self.lock_path or not Path(self.lock_path).is_file():
                raise ToolSetupError("lock_missing", "The pinned dependency list (dependency-lock.json) was not found.",
                                     next_action="Reinstall Rebuild Studio; the file ships next to the application.", status=500)
            self._lock_doc = json.loads(Path(self.lock_path).read_text(encoding="utf-8"))
        return {k: v for k, v in (self._lock_doc.get("tools") or {}).items() if isinstance(v, dict) and v.get("artifact")}

    def _tool(self, name: str) -> dict[str, Any]:
        t = self._tools().get(name)
        if t is None:
            raise ToolSetupError("unknown_tool", f"'{name}' is not a tool Rebuild Studio can install.", affected=name, status=404,
                                 next_action="Pick one of the tools listed in Tools setup.")
        return t

    def _requires(self, name: str) -> list[str]:
        return list((self._tool(name).get("layout") or {}).get("requires") or [])

    def _install_dir(self, name: str) -> Path:
        d = str(self._tool(name).get("install_dir") or name)
        if not d or d != Path(d).name or d.startswith(".") or "/" in d or "\\" in d:
            raise ToolSetupError("bad_lock", f"The lock entry for {name} has an unsafe install_dir.", status=500)
        return self.tools_dir / d

    def _blocked_reason(self, name: str) -> str | None:
        art = self._tool(name)["artifact"]
        if not _is_hex64(art.get("sha256")) or art.get("verify_required"):
            return ("No verified checksum is pinned for this download yet, so Rebuild Studio refuses to install it.")
        url = str(art.get("url") or "")
        try:
            self._check_url(url)
        except ToolSetupError as e:
            return e.message
        return None

    def _check_url(self, url: str) -> None:
        u = urlsplit(url)
        if u.scheme == "https" and u.hostname:
            return
        if self.allow_insecure_loopback and u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
            return
        raise ToolSetupError("insecure_url", f"Only https downloads are allowed (got {url}).", url=url,
                             next_action="Report this to the Rebuild Studio maintainers.")

    # ------------------------------------------------------------------------------------------------ status
    def _marker(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads((self._install_dir(name) / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _entry_ok(self, name: str) -> bool:
        t = self._tool(name)
        layout = t.get("layout") or {}
        root = self._install_dir(name)
        entry = root / str(layout.get("entry") or "")
        try:
            st = entry.stat()
        except OSError:
            return False
        if not entry.is_file():
            return False
        for extra in (layout.get("extra_files") or {}):
            if not (root / extra).is_file():
                return False
        for made in ((layout.get("post_install") or {}).get("produces") or []):
            if not (root / made).is_file():
                return False
        want = layout.get("entry_sha256")
        if not want:
            return True
        key = (str(entry), st.st_mtime_ns, st.st_size)
        if key not in self._verified:
            try:
                self._verified[key] = sha256_file(entry).lower() == str(want).lower()
            except OSError:
                self._verified[key] = False
        return self._verified[key]

    def _disk_status(self, name: str) -> str:
        root = self._install_dir(name)
        t = self._tool(name)
        if not root.exists():
            return "blocked_unverified" if self._blocked_reason(name) else "not_installed"
        marker = self._marker(name)
        if marker is None:
            return "installed" if self._entry_ok(name) else "corrupt"   # adopted install (e.g. from the PowerShell script)
        if not self._entry_ok(name):
            return "corrupt"
        art = t["artifact"]
        if marker.get("version") != t.get("version") or (marker.get("sha256") or "").lower() != str(art.get("sha256") or "").lower():
            return "update_available"
        return "installed"

    def status(self, name: str) -> dict[str, Any]:
        t = self._tool(name)
        art = t["artifact"]
        with self._lock:
            job = self._jobs.get(name)
            job_view = self._job_view(job, name) if job else None
        disk = self._disk_status(name)
        status = disk
        if job_view and not job_view["finished"]:
            status = "installing"
        title, purpose = FRIENDLY.get(name, (name, str(t.get("role") or "")))
        blocked = self._blocked_reason(name)
        return {
            "name": name, "title": title, "purpose": purpose, "role": t.get("role"), "version": t.get("version"),
            "license": t.get("license"), "optional": bool(t.get("optional")), "size_bytes": art.get("size_bytes"),
            "file_name": art.get("name"), "url": art.get("url"), "sha256": art.get("sha256"),
            "footprint": t.get("footprint"), "requires": self._requires(name), "status": status, "disk_status": disk,
            "blocked_reason": blocked if disk in ("blocked_unverified",) or (blocked and disk != "installed") else None,
            "install_path": str(self._install_dir(name)), "installed_version": (self._marker(name) or {}).get("version"),
            "job": job_view,
        }

    def _job_view(self, job: _Job, name: str) -> dict[str, Any]:
        pct = None
        if job.bytes_total:
            pct = round(100 * job.bytes_done / job.bytes_total, 1)
        return {"phase": job.phase, "bytes_done": job.bytes_done, "bytes_total": job.bytes_total, "percent": pct,
                "message": job.message, "error": job.error, "finished": job.finished, "chain": list(job.chain),
                "cancelled": job.phase == "cancelled"}

    def snapshot(self) -> dict[str, Any]:
        names = list(self._tools())
        tools = [self.status(n) for n in names]
        required = [t for t in tools if not t["optional"]]
        return {
            "tools_dir": str(self.tools_dir), "lock_path": str(self.lock_path) if self.lock_path else None,
            "tools": tools,
            "any_installed": any(t["status"] in ("installed", "update_available") for t in tools),
            "required_missing": [t["name"] for t in required if t["status"] not in ("installed", "update_available")],
            "busy": self._busy(),
        }

    def _busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------------------------------------ public actions
    def _healthy(self, name: str) -> bool:
        return self._disk_status(name) in ("installed", "update_available")

    def _chain_for(self, name: str, *, deps_must_exist: bool) -> list[str]:
        order: list[str] = []

        def visit(n: str, stack: tuple[str, ...]) -> None:
            if n in stack:
                raise ToolSetupError("bad_lock", f"Circular tool requirement: {' -> '.join(stack + (n,))}", status=500)
            self._tool(n)
            for r in self._requires(n):
                try:
                    self._tool(r)
                except ToolSetupError:
                    raise ToolSetupError("bad_lock", f"{n} requires unknown tool {r}.", status=500) from None
                visit(r, stack + (n,))
            if n not in order:
                order.append(n)

        visit(name, ())
        out = []
        for n in order:
            if n == name or not self._healthy(n):
                out.append(n)
        for n in out:
            if n == name:
                continue
            title = FRIENDLY.get(n, (n, ""))[0]
            if deps_must_exist:
                raise ToolSetupError("dependency_missing", f"{FRIENDLY.get(name, (name,))[0]} needs {title} first, and it is not installed.",
                                     affected=n, status=409, next_action=f"Install {title} first (it can also be installed from a file).")
            reason = self._blocked_reason(n)
            if reason:
                raise ToolSetupError("dependency_blocked", f"{title} is required but cannot be installed: {reason}", affected=n, status=409)
        return out

    def install(self, name: str, *, local_file: str | Path | None = None, force: bool = False) -> dict[str, Any]:
        t = self._tool(name)
        reason = self._blocked_reason(name)
        if reason:
            raise ToolSetupError("blocked_unverified", f"{FRIENDLY.get(name, (name,))[0]}: {reason}", affected=name, status=409,
                                 next_action="Nothing was installed. A release with a verified checksum is needed.")
        src: Path | None = None
        if local_file is not None:
            src = Path(str(local_file).strip().strip('"'))
            if not src.is_file():
                raise ToolSetupError("file_not_found", f"'{src}' is not a file.", affected=str(src), status=400,
                                     next_action=f"Choose the downloaded file ({t['artifact'].get('name')}).")
        if self._disk_status(name) == "installed" and not force:
            return self.status(name)
        chain = self._chain_for(name, deps_must_exist=src is not None)
        with self._lock:
            if self._busy():
                raise ToolSetupError("busy", "Another tool is being installed. Wait for it to finish or cancel it.", status=409,
                                     next_action="Wait for the running install, or press Cancel on it.")
            cancel = threading.Event()
            for n in chain:
                self._jobs[n] = _Job(chain=list(chain), cancel=cancel, message="Waiting to start")
            th = threading.Thread(target=self._run_chain, args=(name, chain, cancel, src), name=f"tool-setup-{name}", daemon=True)
            self._thread = th
            th.start()
        self._emit("tools.setup.started", {"tool": name, "chain": chain})
        return self.status(name)

    def install_from_file(self, name: str, path: str | Path) -> dict[str, Any]:
        return self.install(name, local_file=path, force=True)

    def cancel(self, name: str) -> dict[str, Any]:
        self._tool(name)
        with self._lock:
            job = self._jobs.get(name)
            if job is None or job.finished:
                raise ToolSetupError("not_running", f"{name} is not being installed.", affected=name, status=409,
                                     next_action="Nothing to cancel.")
            job.cancel.set()
        return self.status(name)

    def remove(self, name: str) -> dict[str, Any]:
        self._tool(name)
        with self._lock:
            job = self._jobs.get(name)
            if (job and not job.finished) or self._busy():
                raise ToolSetupError("busy", "An install is running. Cancel it before removing tools.", affected=name, status=409)
        for other in self._tools():
            if other != name and name in self._requires(other) and self._install_dir(other).exists():
                raise ToolSetupError("in_use", f"{FRIENDLY.get(other, (other,))[0]} needs this tool.", affected=other, status=409,
                                     next_action=f"Remove {FRIENDLY.get(other, (other,))[0]} first.")
        root = self._install_dir(name)
        if root.exists():
            trash = self.tools_dir / ".staging" / f"removed-{root.name}-{uuid.uuid4().hex[:8]}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(root, trash)
            except OSError as e:
                raise ToolSetupError("remove_failed", f"Could not remove {root}: {e}", affected=str(root), status=409,
                                     next_action="Close any analysis that is using the tool and try again.") from e
            _rmtree(trash)
        with self._lock:
            self._jobs.pop(name, None)
        self._emit("tools.setup.removed", {"tool": name})
        return self.status(name)

    def join(self, timeout: float | None = None) -> bool:
        th = self._thread
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()

    # ------------------------------------------------------------------------------------------------ events/progress
    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, payload)
        except Exception:  # noqa: BLE001 - progress events must never break an install
            pass

    def _progress(self, name: str, *, phase: str | None = None, done: int | None = None, total: int | None = None,
                  message: str | None = None, force_emit: bool = False) -> None:
        with self._lock:
            job = self._jobs.get(name)
            if job is None:
                return
            changed = False
            if phase is not None and phase != job.phase:
                job.phase = phase
                changed = True
            if done is not None:
                job.bytes_done = done
            if total is not None:
                job.bytes_total = total
            if message is not None:
                job.message = message
            now = time.monotonic()
            emit = changed or force_emit or now - job.last_emit > 0.5
            if emit:
                job.last_emit = now
            payload = {"tool": name, "phase": job.phase, "bytes_done": job.bytes_done, "bytes_total": job.bytes_total,
                       "message": job.message}
        if emit:
            self._emit("tools.setup.progress", payload)

    # ------------------------------------------------------------------------------------------------ worker
    def _run_chain(self, requested: str, chain: list[str], cancel: threading.Event, local: Path | None) -> None:
        current = chain[0] if chain else requested
        try:
            for n in chain:
                current = n
                if cancel.is_set():
                    raise _Cancelled()
                self._install_one(n, cancel, local if n == requested else None)
                with self._lock:
                    j = self._jobs.get(n)
                    if j:
                        j.phase, j.finished, j.message, j.error = "done", True, "Installed", None
                self._emit("tools.setup.done", {"tool": n})
            with self._lock:
                for n in chain:   # a successful chain leaves no job record; status comes from disk
                    self._jobs.pop(n, None)
        except _Cancelled:
            with self._lock:
                for n in chain:
                    j = self._jobs.get(n)
                    if j and not j.finished:
                        j.phase, j.finished, j.message = "cancelled", True, "Cancelled. Nothing was installed."
            self._emit("tools.setup.cancelled", {"tool": requested})
        except ToolSetupError as e:
            self._fail(chain, current, requested, e)
        except Exception as e:  # noqa: BLE001
            log.exception("tool setup failed")
            self._fail(chain, current, requested, ToolSetupError("internal", f"Unexpected error: {type(e).__name__}: {e}",
                                                                  retryable=True, next_action="Try again; if it keeps failing, check the logs."))

    def _fail(self, chain: list[str], current: str, requested: str, e: ToolSetupError) -> None:
        with self._lock:
            for n in chain:
                j = self._jobs.get(n)
                if not j:
                    continue
                if n == current:
                    j.phase, j.finished, j.error, j.message = "failed", True, e.to_dict(), e.message
                elif not j.finished:
                    dep = FRIENDLY.get(current, (current,))[0]
                    err = ToolSetupError("dependency_failed", f"A required component ({dep}) could not be installed: {e.message}",
                                         affected=current, retryable=e.retryable, next_action=e.next_action, url=e.url)
                    j.phase, j.finished, j.error, j.message = "failed", True, err.to_dict(), err.message
        self._emit("tools.setup.failed", {"tool": current, "error": e.to_dict()})

    # ------------------------------------------------------------------------------------------------ one tool
    def _install_one(self, name: str, cancel: threading.Event, local: Path | None) -> None:
        t = self._tool(name)
        art = t["artifact"]
        layout = t.get("layout") or {}
        final = self._install_dir(name)
        dl_dir = self.tools_dir / ".downloads"
        stage_root = self.tools_dir / ".staging"
        dl_dir.mkdir(parents=True, exist_ok=True)
        stage_root.mkdir(parents=True, exist_ok=True)
        part = dl_dir / f"{art['name']}.part"
        staged = stage_root / f"{final.name}-{uuid.uuid4().hex[:8]}"
        old_backup: Path | None = None
        keep_part = False
        try:
            for stale in stage_root.glob(f"{final.name}-*"):     # leftovers of a crashed run
                _rmtree(stale)
            resume_from = self._resumable_bytes(part, art) if local is None else 0
            if not resume_from:
                _drop_part(part)
            self._progress(name, phase="downloading", done=resume_from, total=int(art.get("size_bytes") or 0),
                           message="Copying the file" if local else ("Resuming the download" if resume_from else "Downloading"),
                           force_emit=True)
            if local is not None:
                self._copy_local(name, local, part, art, cancel)
            else:
                try:
                    self._download(name, str(art["url"]), part, art, cancel, resume_from=resume_from)
                except ToolSetupError as e:     # a dropped connection keeps the verified-so-far bytes for the next try
                    keep_part = e.retryable and e.code in ("offline", "download_failed") and part.is_file() and part.stat().st_size > 0
                    raise
            self._verify_archive(name, part, art, local is not None)
            if cancel.is_set():
                raise _Cancelled()
            self._progress(name, phase="extracting", message="Unpacking", force_emit=True)
            staged.mkdir(parents=True)
            if str(art.get("format") or "zip") == "file":      # single-file artifact (e.g. cfr-*.jar): copy, never unzip
                target = safe_archive_target(staged, str(layout["entry"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(part, target)
            else:
                self._extract(name, part, staged, str(layout.get("archive_root") or ""), cancel)
            self._verify_layout(name, staged, layout)
            self._progress(name, phase="checking", message="Checking that the tool starts", force_emit=True)
            self._version_check(name, staged, layout)
            if layout.get("post_install"):
                self._post_install(name, staged, layout["post_install"], cancel)
            marker = {"name": name, "version": t.get("version"), "sha256": str(art["sha256"]).lower(),
                      "entry_sha256": layout.get("entry_sha256"), "installed_at": now_iso(),
                      "source": "file" if local else "download"}
            (staged / MARKER).write_text(json.dumps(marker, indent=2), encoding="utf-8")
            if cancel.is_set():
                raise _Cancelled()
            self._progress(name, phase="activating", message="Finishing", force_emit=True)
            if final.exists():
                old_backup = stage_root / f"old-{final.name}-{uuid.uuid4().hex[:8]}"
                try:
                    os.rename(final, old_backup)
                except OSError as e:
                    old_backup = None
                    raise ToolSetupError("in_use", f"Cannot replace the existing {final.name} folder ({e}).", retryable=True,
                                         next_action="Close any analysis or preview that is running, then retry.") from e
            try:
                os.rename(staged, final)
            except OSError as e:
                if old_backup and old_backup.exists():
                    os.rename(old_backup, final)
                    old_backup = None
                raise ToolSetupError("activate_failed", f"Could not move the tool into place: {e}", retryable=True,
                                     next_action="Close programs that may lock the tools folder and retry.") from e
            self._verified.clear()
        finally:
            if not keep_part:
                _drop_part(part)
            if staged.exists():
                _rmtree(staged)
            if old_backup and old_backup.exists():
                _rmtree(old_backup)

    # -- acquiring bytes --------------------------------------------------------------------------------------------
    def _offline_error(self, url: str, art: dict[str, Any], why: str) -> ToolSetupError:
        host = urlsplit(url).hostname or url
        return ToolSetupError(
            "offline",
            f"Could not reach {host} ({why}). Download URL: {url}",
            affected=url, retryable=True, url=url,
            next_action=(f"Check your internet connection and retry. Or download {art.get('name')} on a computer that is online "
                         f"and choose it with 'Install from file…' (expected SHA-256 {art.get('sha256')})."))

    def _resumable_bytes(self, part: Path, art: dict[str, Any]) -> int:
        """Bytes of an interrupted download of exactly this pinned artifact that can be continued (0 = start over)."""
        try:
            meta = json.loads(_part_meta(part).read_text(encoding="utf-8"))
            size = part.stat().st_size
        except (OSError, ValueError):
            return 0
        expected = int(art.get("size_bytes") or 0)
        same = meta.get("sha256") == str(art.get("sha256") or "").lower() and meta.get("url") == art.get("url")
        return size if same and 0 < size < (expected or size + 1) else 0

    def _download(self, name: str, url: str, part: Path, art: dict[str, Any], cancel: threading.Event, *, resume_from: int = 0) -> None:
        expected = int(art.get("size_bytes") or 0)
        sha512 = art.get("sha512_official")
        h256, h512 = hashlib.sha256(), (hashlib.sha512() if sha512 else None)
        if resume_from:          # the kept bytes are hashed again here; the final checksum still covers the whole file
            with open(part, "rb") as f:
                for b in iter(lambda: f.read(CHUNK), b""):
                    h256.update(b)
                    if h512:
                        h512.update(b)
        cur = url
        timeout = httpx.Timeout(30.0, read=60.0)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": "RebuildStudio-ToolSetup/1"}) as client:
                for _ in range(6):
                    self._check_url(cur)
                    with client.stream("GET", cur, headers={"Range": f"bytes={resume_from}-"} if resume_from else None) as r:
                        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                            cur = urljoin(cur, r.headers["location"])
                            continue
                        resumed = bool(resume_from) and r.status_code == 206 and                             str(r.headers.get("content-range") or "").startswith(f"bytes {resume_from}-")
                        if resume_from and not resumed:   # server ignored the range: start over from byte 0
                            resume_from = 0
                            h256, h512 = hashlib.sha256(), (hashlib.sha512() if sha512 else None)
                            if r.status_code in (206, 416):
                                _drop_part(part)
                                continue
                        if r.status_code != 200 and not resumed:
                            raise ToolSetupError("download_failed", f"The download server answered HTTP {r.status_code} for {url}.",
                                                 affected=url, retryable=r.status_code >= 500 or r.status_code in (408, 429), url=url,
                                                 next_action=f"Try again in a few minutes, or download {art.get('name')} elsewhere and use 'Install from file…'.")
                        done = resume_from if resumed else 0
                        total = expected or (done + int(r.headers.get("content-length") or 0))
                        self._progress(name, total=total, done=done)
                        _part_meta(part).write_text(json.dumps({"url": art.get("url"), "sha256": str(art.get("sha256") or "").lower()}),
                                                    encoding="utf-8")
                        with open(part, "ab" if resumed else "wb") as f:
                            for chunk in r.iter_bytes(CHUNK):
                                if cancel.is_set():
                                    raise _Cancelled()
                                done += len(chunk)
                                if expected and done > expected:
                                    raise ToolSetupError("size_mismatch", f"The download is larger than the pinned size ({_fmt_mb(expected)}); nothing was installed.",
                                                         affected=url, retryable=False, url=url,
                                                         next_action="Do not use this file. Report it, or download from another source and use 'Install from file…'.")
                                f.write(chunk)
                                h256.update(chunk)
                                if h512:
                                    h512.update(chunk)
                                self._progress(name, done=done)
                        self._progress(name, done=done, force_emit=True)
                        self._remember_hashes(part, h256.hexdigest(), h512.hexdigest() if h512 else None)
                        return
                raise ToolSetupError("download_failed", f"Too many redirects while downloading {url}.", affected=url, retryable=True, url=url)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise self._offline_error(url, art, type(e).__name__) from e
        except socket.gaierror as e:
            raise self._offline_error(url, art, "DNS lookup failed") from e
        except httpx.TransportError as e:
            raise ToolSetupError("download_failed", f"The connection dropped while downloading {url} ({type(e).__name__}).",
                                 affected=url, retryable=True, url=url,
                                 next_action="Retry. A partial download was discarded.") from e

    def _copy_local(self, name: str, src: Path, part: Path, art: dict[str, Any], cancel: threading.Event) -> None:
        sha512 = art.get("sha512_official")
        h256, h512 = hashlib.sha256(), (hashlib.sha512() if sha512 else None)
        total = src.stat().st_size
        self._progress(name, total=total, done=0)
        done = 0
        try:
            with open(src, "rb") as fi, open(part, "wb") as fo:
                while True:
                    if cancel.is_set():
                        raise _Cancelled()
                    b = fi.read(CHUNK)
                    if not b:
                        break
                    fo.write(b)
                    h256.update(b)
                    if h512:
                        h512.update(b)
                    done += len(b)
                    self._progress(name, done=done)
        except OSError as e:
            raise ToolSetupError("file_unreadable", f"Could not read {src}: {e}", affected=str(src),
                                 next_action="Check the file is not open in another program and retry.") from e
        self._remember_hashes(part, h256.hexdigest(), h512.hexdigest() if h512 else None)

    def _remember_hashes(self, part: Path, s256: str, s512: str | None) -> None:
        self._hashes[str(part)] = (s256, s512)

    def _verify_archive(self, name: str, part: Path, art: dict[str, Any], from_file: bool) -> None:
        self._progress(name, phase="verifying", message="Verifying checksum", force_emit=True)
        s256, s512 = self._hashes.pop(str(part), (None, None))
        if s256 is None:
            s256 = sha256_file(part)
        size = part.stat().st_size
        want_size = art.get("size_bytes")
        what = "The file you chose" if from_file else "The downloaded file"
        if want_size and size != int(want_size):
            raise ToolSetupError("size_mismatch", f"{what} is {size} bytes but {int(want_size)} bytes were expected; nothing was installed.",
                                 affected=str(art.get("name")), retryable=not from_file,
                                 next_action="Retry the download, or get the file again from the official page.")
        if s256.lower() != str(art["sha256"]).lower():
            raise ToolSetupError("checksum_mismatch", f"{what} does not match the pinned checksum; nothing was installed.",
                                 affected=str(art.get("name")), retryable=not from_file,
                                 next_action=("The file was deleted. Retry the download; if it fails again do not use this file."
                                              if not from_file else "Choose the original, unmodified file from the official download page."))
        if art.get("sha512_official") and (s512 or "").lower() != str(art["sha512_official"]).lower():
            raise ToolSetupError("checksum_mismatch", f"{what} does not match the vendor's published SHA-512; nothing was installed.",
                                 affected=str(art.get("name")), retryable=not from_file,
                                 next_action="The file was deleted. Retry; if it fails again do not use this file.")

    # -- extraction -------------------------------------------------------------------------------------------------
    def _extract_tar(self, archive: Path, staged: Path, root_parts: list[str], cancel: threading.Event) -> None:
        """.tgz artifacts (npm packages). Same rules as zip: refuse links/devices/unsafe paths before writing anything."""
        limits = self.settings.limits
        try:
            tf = tarfile.open(archive, "r:*")
        except tarfile.TarError as e:
            raise ToolSetupError("bad_archive", "The archive is not a valid tar file; nothing was installed.", retryable=False) from e
        with tf:
            members = tf.getmembers()
            if len(members) > limits.max_archive_entries or sum(m.size for m in members) > limits.max_archive_expansion_bytes:
                raise ToolSetupError("unsafe_archive", "The archive is larger than the safety limit; nothing was installed.")
            for m in members:
                if not (m.isfile() or m.isdir()):
                    raise ToolSetupError("unsafe_archive", f"The archive contains a link or special file ({m.name}); nothing was installed.",
                                         affected=m.name, next_action="Do not use this file.")
                try:
                    safe_archive_target(staged, m.name)
                except PathPolicyError as e:
                    raise ToolSetupError("unsafe_archive", f"The archive contains an unsafe path ({m.name}); nothing was installed.",
                                         affected=m.name, next_action="Do not use this file.") from e
            for m in members:
                if cancel.is_set():
                    raise _Cancelled()
                parts = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".")]
                if root_parts:
                    if parts[:len(root_parts)] != root_parts:
                        continue
                    parts = parts[len(root_parts):]
                if not parts:
                    continue
                target = safe_archive_target(staged, "/".join(parts))
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)

    def _extract(self, name: str, archive: Path, staged: Path, archive_root: str, cancel: threading.Event) -> None:
        limits = self.settings.limits
        root_parts = [p for p in archive_root.replace("\\", "/").split("/") if p]
        if not zipfile.is_zipfile(archive) and tarfile.is_tarfile(archive):
            return self._extract_tar(archive, staged, root_parts, cancel)
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile as e:
            raise ToolSetupError("bad_archive", "The archive is not a valid zip file; nothing was installed.", retryable=False) from e
        with zf:
            infos = zf.infolist()
            if len(infos) > limits.max_archive_entries:
                raise ToolSetupError("unsafe_archive", "The archive has too many entries; nothing was installed.")
            if sum(i.file_size for i in infos) > limits.max_archive_expansion_bytes:
                raise ToolSetupError("unsafe_archive", "The archive would expand beyond the safety limit; nothing was installed.")
            for i in infos:                                   # pass 1: refuse the whole archive on any unsafe member
                try:
                    safe_archive_target(staged, i.filename)
                except PathPolicyError as e:
                    raise ToolSetupError("unsafe_archive", f"The archive contains an unsafe path ({i.filename}); nothing was installed.",
                                         affected=i.filename, next_action="Do not use this file.") from e
                if stat.S_ISLNK((i.external_attr >> 16) & 0xFFFF):
                    raise ToolSetupError("unsafe_archive", f"The archive contains a symbolic link ({i.filename}); nothing was installed.",
                                         affected=i.filename, next_action="Do not use this file.")
            written = 0
            for i in infos:                                   # pass 2: extract under archive_root
                parts = [p for p in i.filename.replace("\\", "/").split("/") if p not in ("", ".")]
                if root_parts:
                    if parts[:len(root_parts)] != root_parts:
                        continue
                    parts = parts[len(root_parts):]
                if not parts:
                    continue
                target = safe_archive_target(staged, "/".join(parts))
                if i.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                budget = i.file_size
                with zf.open(i) as src, open(target, "wb") as dst:
                    while True:
                        if cancel.is_set():
                            raise _Cancelled()
                        b = src.read(CHUNK)
                        if not b:
                            break
                        budget -= len(b)
                        written += len(b)
                        if budget < 0 or written > limits.max_archive_expansion_bytes:
                            raise ToolSetupError("unsafe_archive", f"{i.filename} is larger than the archive declares; nothing was installed.")
                        dst.write(b)

    def _verify_layout(self, name: str, staged: Path, layout: dict[str, Any]) -> None:
        entry_rel = str(layout.get("entry") or "")
        entry = staged / entry_rel
        if not entry_rel or not entry.is_file():
            raise ToolSetupError("layout_mismatch", f"The expected program ({entry_rel}) is missing from the archive; nothing was installed.",
                                 affected=entry_rel)
        want = layout.get("entry_sha256")
        if want and sha256_file(entry).lower() != str(want).lower():
            raise ToolSetupError("checksum_mismatch", f"{entry_rel} inside the archive does not match its pinned checksum; nothing was installed.",
                                 affected=entry_rel)
        for rel, h in (layout.get("extra_files") or {}).items():
            f = staged / rel
            if not f.is_file():
                raise ToolSetupError("layout_mismatch", f"{rel} is missing from the archive; nothing was installed.", affected=rel)
            if sha256_file(f).lower() != str(h).lower():
                raise ToolSetupError("checksum_mismatch", f"{rel} inside the archive does not match its pinned checksum; nothing was installed.", affected=rel)

    # -- where downloaded content is executed (see the module docstring) -------------------------------------------
    def dotnet_exe(self) -> Path:
        return self.tools_dir / "dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet")

    def _version_check(self, name: str, staged: Path, layout: dict[str, Any]) -> None:
        entry = staged / str(layout["entry"])
        args = list(layout.get("version_args") or [])
        launch = str(layout.get("launch") or "")
        env = dict(os.environ)
        env.update({"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1"})
        if launch.startswith("java ") or "temurin-jre" in (layout.get("requires") or []):
            jre = self.tools_dir / "jre"
            java = jre / ("bin/java.exe" if os.name == "nt" else "bin/java")
            if not java.is_file():
                raise ToolSetupError("dependency_missing", "The private Java runtime is not installed.", affected="temurin-jre", status=409)
            env["JAVA_HOME"] = str(jre)
        if launch.startswith("java "):
            cmd = [str(java), *launch.split()[1:], str(entry), *(args or ["--version"])]
        elif launch.startswith("dotnet "):
            dn = self.dotnet_exe()
            if not dn.is_file():
                raise ToolSetupError("dependency_missing", "The private .NET runtime is not installed.", affected="dotnet-runtime", status=409)
            env["DOTNET_ROOT"] = str(dn.parent)
            env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
            cmd = [str(dn), str(entry), *(args or ["--version"])]
        elif args:
            cmd = [str(entry), *args]
            if entry.name.lower() == "dotnet.exe" or entry.name == "dotnet":
                env["DOTNET_ROOT"] = str(entry.parent)
        else:
            return
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=90, shell=False, stdin=subprocess.DEVNULL, env=env, cwd=str(staged))
        except subprocess.TimeoutExpired as e:
            raise ToolSetupError("version_check_failed", "The tool did not answer its version check in time; nothing was installed.",
                                 retryable=True, next_action="Retry. If it repeats, security software may be blocking the tool.") from e
        except OSError as e:
            raise ToolSetupError("version_check_failed", f"The tool could not be started ({e}); nothing was installed.",
                                 next_action="Check that your antivirus is not blocking Rebuild Studio's tools folder, then retry.") from e
        out = (r.stdout or b"") + (r.stderr or b"")
        if r.returncode != 0 and not out.strip():   # Godot-based tools print the version and exit 1
            tail = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()[:300]
            raise ToolSetupError("version_check_failed", f"The tool's version check failed (exit {r.returncode}): {tail}; nothing was installed.",
                                 next_action="Retry; if it repeats, report the message above.")

    # -- the second (and last) place downloaded content is executed -------------------------------------------------
    def _post_install(self, name: str, staged: Path, spec: dict[str, Any], cancel: threading.Event) -> None:
        """Run the lock-declared installer step on the staged copy. Failure/cancel/timeout raise, so nothing is activated."""
        def expand(v: str) -> str:
            return str(v).replace("{staged}", str(staged)).replace("{tools}", str(self.tools_dir))

        argv = [expand(a) for a in (spec.get("argv") or [])]
        if not argv:
            raise ToolSetupError("bad_lock", f"The lock entry for {name} declares an empty post_install.", status=500)
        prog = Path(argv[0])
        try:
            prog.resolve().relative_to(staged.resolve())
        except ValueError:
            raise ToolSetupError("bad_lock", f"post_install for {name} must run a file inside the staged download, not {prog}.", status=500) from None
        if not prog.is_file():
            raise ToolSetupError("layout_mismatch", f"The installer program ({prog.name}) is missing; nothing was installed.", affected=prog.name)
        env = dict(os.environ)
        env.update({str(k): expand(v) for k, v in (spec.get("env") or {}).items()})
        timeout = float(spec.get("timeout_seconds") or 1800)
        message = str(spec.get("progress_message") or "Installing")
        log_name = Path(str(spec.get("log_name") or f"{name}-install.log")).name
        log_dir = self.tools_dir / ".logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / log_name
        self._progress(name, phase="installing", message=message, force_emit=True)

        def tail(n: int = 600) -> str:
            try:
                with open(log_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - n))
                    raw = f.read().decode("utf-8", "replace")
            except OSError:
                return ""
            parts = [p.strip() for p in raw.replace("\r", "\n").split("\n") if p.strip()]
            return parts[-1][:160] if parts else ""

        kw: dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            with open(log_path, "wb") as logf:
                logf.write(f"$ {' '.join(argv)}\n".encode("utf-8", "replace"))
                logf.flush()
                try:
                    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, env=env,
                                            cwd=str(staged), shell=False, **kw)
                except OSError as e:
                    raise ToolSetupError("post_install_failed", f"The installer could not be started ({e}); nothing was installed.",
                                         next_action="Check that your antivirus is not blocking Rebuild Studio's tools folder, then retry.") from e
                started = time.monotonic()
                try:
                    while True:
                        try:
                            rc = proc.wait(timeout=0.25)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                        if cancel.is_set():
                            _kill_tree(proc)
                            proc.wait(timeout=30)
                            raise _Cancelled()
                        if time.monotonic() - started > timeout:
                            _kill_tree(proc)
                            proc.wait(timeout=30)
                            raise ToolSetupError("post_install_timeout", f"The installer did not finish in time ({timeout:g} s); nothing was installed.",
                                                 retryable=True, next_action=f"Retry (the log is at {log_path}). A slow or filtered network can cause this.")
                        self._progress(name, message=f"{message} {tail()}".strip())
                finally:
                    if proc.poll() is None:
                        _kill_tree(proc)
        except OSError as e:
            raise ToolSetupError("post_install_failed", f"Could not write the install log {log_path}: {e}", retryable=True) from e
        if rc != 0:
            last = tail(2000)
            raise ToolSetupError("post_install_failed", f"The installer failed (exit {rc}){': ' + last if last else ''}; nothing was installed.",
                                 retryable=True, next_action=f"Check your internet connection and retry. Details: {log_path}")
        for rel in spec.get("produces") or []:
            if not (staged / rel).is_file():
                raise ToolSetupError("post_install_failed", f"The installer finished but {rel} was not created; nothing was installed.",
                                     retryable=True, affected=rel, next_action=f"Retry. Details: {log_path}")
