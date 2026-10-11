"""R11 data safety: the project database cannot be lost.

  * ``StoreLock``      - one writer per data folder. The serving controller holds ``<data>/studio.lock`` (an OS byte-range /
                         flock lock that dies with the process); the CLI and ``rebuild-mcp`` take the same lock before they
                         open SQLite directly, and only when no controller is alive (otherwise they forward over HTTP).
  * package guard      - a process with Windows package identity (MSIX; e.g. started by Claude Desktop) sees a redirected,
                         private copy of %LOCALAPPDATA%, so SQLite's -wal/-shm coordination with the real app does not line
                         up. Such a process never opens the store directly.
  * backups            - SQLite online-backup copies under ``<data>/backups/``, each verified with ``PRAGMA integrity_check``;
                         the newest 5 plus the newest of each of the last 7 days are kept.
  * startup integrity  - ``PRAGMA quick_check`` before the services start; a damaged store makes the controller exit with
                         ``EXIT_STORE_DAMAGED`` after writing ``store-recovery.json`` for the desktop shell's recovery dialog.
  * recovery           - restore a backup or start empty; the damaged files are moved to ``<data>/damaged/``, never deleted.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

EXIT_STORE_DAMAGED = 70      # store failed its integrity check; store-recovery.json describes the choices
EXIT_DATA_IN_USE = 75        # another process holds the data-folder lock
EXIT_PACKAGED = 78           # refused: running inside a Windows package context

DB_NAME = "studio.sqlite3"
LOCK_NAME = "studio.lock"
LOCK_INFO = "studio.lock.json"
YIELD_NAME = "studio.lock.yield"
RECOVERY_FILE = "store-recovery.json"
BACKUP_DIR = "backups"
DAMAGED_DIR = "damaged"
KEEP_LAST = 5
KEEP_DAYS = 7
PERIODIC_SECONDS = 6 * 3600
ENV_RECOVER = "REBUILD_STUDIO_RECOVER"          # set by the desktop shell after the user's choice: "restore:<name>" | "empty"
ENV_TEST_NO_PACKAGE_GUARD = "REBUILD_STUDIO_TEST_NO_PACKAGE_GUARD"   # honoured only in test/script contexts (see config)

_NAME_RE = re.compile(r"^studio-(\d{8}T\d{6}Z)-([a-z0-9_-]{1,32})(?:-\d+)?\.sqlite3$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


# ============================================================================================ package-context guard
APPMODEL_ERROR_NO_PACKAGE = 15700
ERROR_INSUFFICIENT_BUFFER = 122

_package_probe: Optional[Callable[[], Optional[str]]] = None


def set_package_probe(probe: Optional[Callable[[], Optional[str]]]) -> None:
    """Replace the package-identity probe (tests). ``None`` restores the real GetCurrentPackageFullName query."""
    global _package_probe
    _package_probe = probe


def query_package_full_name(fn: Callable[..., int]) -> Optional[str]:
    """Run the GetCurrentPackageFullName(UINT32* length, PWSTR name) protocol against ``fn``: None when the process has no
    package identity (APPMODEL_ERROR_NO_PACKAGE) or on any unexpected answer, else the package full name."""
    length = ctypes.c_uint32(0)
    rc = fn(ctypes.byref(length), None)
    if rc == APPMODEL_ERROR_NO_PACKAGE:
        return None
    if rc != ERROR_INSUFFICIENT_BUFFER or length.value == 0:
        return None
    buf = ctypes.create_unicode_buffer(length.value)
    rc = fn(ctypes.byref(length), buf)
    return (buf.value or "unknown-package") if rc == 0 else None


def package_full_name() -> Optional[str]:
    """The Windows package full name of this process, or None (no package identity / not Windows / API missing)."""
    if _package_probe is not None:
        return _package_probe()
    from ..config import test_context
    if test_context() and os.environ.get(ENV_TEST_NO_PACKAGE_GUARD) == "1":
        return None
    if sys.platform != "win32":
        return None
    try:
        fn = ctypes.WinDLL("kernel32").GetCurrentPackageFullName  # Windows 8+
    except (OSError, AttributeError):  # pragma: no cover - very old Windows
        return None
    return query_package_full_name(fn)


class PackagedClientRefused(RuntimeError):
    """Direct store access from a process with Windows package identity."""

    def __init__(self, package: str, what: str = "this program"):
        self.package = package
        super().__init__(
            f"{what} is running inside a Windows app package ({package}), for example because Claude Desktop started it. "
            "Windows gives such programs a private, redirected copy of the AppData folder, so opening the Rebuild Studio "
            "database from here could damage it. Use the running desktop app instead: in Rebuild Studio open Settings -> "
            "'Use Rebuild Studio from an AI client' and copy the HTTP configuration (it talks to the app's /mcp endpoint).")


def refuse_if_packaged(what: str = "this program") -> None:
    pkg = package_full_name()
    if pkg:
        raise PackagedClientRefused(pkg, what)


# ============================================================================================ one writer per data folder
class DataFolderInUse(RuntimeError):
    def __init__(self, data_dir: Path, holder: Optional[dict[str, Any]]):
        self.data_dir, self.holder = Path(data_dir), holder or {}
        who = describe_holder(self.holder)
        super().__init__(f"The Rebuild Studio data folder {data_dir} is in use by {who}. Only one program may open the project "
                         "database at a time.")


def describe_holder(h: Optional[dict[str, Any]]) -> str:
    if not h:
        return "another Rebuild Studio process"
    role = {"controller": "the Rebuild Studio app (controller)", "mcp": "rebuild-mcp (an AI client's MCP server)",
            "cli": "rebuildctl"}.get(str(h.get("role")), str(h.get("role") or "another Rebuild Studio process"))
    pid = h.get("pid")
    return f"{role}, pid {pid}" if pid else role


class StoreLock:
    """Exclusive, process-lifetime lock on ``<data>/studio.lock``. Never deleted; the OS drops it when the holder exits."""

    def __init__(self, data_dir: Path | str, role: str):
        self.data_dir = Path(data_dir)
        self.role = role
        self.path = self.data_dir / LOCK_NAME
        self.info_path = self.data_dir / LOCK_INFO
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _try_lock(self) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(self, wait: float = 0.0, poll: float = 0.2, *, request_yield: bool = False) -> bool:
        """Take the lock, waiting up to ``wait`` seconds. With ``request_yield`` a direct-mode holder (CLI / rebuild-mcp)
        is asked to let go (it does so between tool calls)."""
        if self._fd is not None:
            return True
        deadline = time.monotonic() + max(0.0, wait)
        asked = False
        while True:
            if self._try_lock():
                self._write_info()
                if asked:
                    _unlink(self.data_dir / YIELD_NAME)
                return True
            if request_yield and not asked:
                try:
                    (self.data_dir / YIELD_NAME).write_text(json.dumps({"role": self.role, "pid": os.getpid(),
                                                                        "at": _now().isoformat()}), "utf-8")
                    asked = True
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                if asked:
                    _unlink(self.data_dir / YIELD_NAME)
                return False
            time.sleep(poll)

    def require(self, wait: float = 0.0, *, request_yield: bool = False) -> "StoreLock":
        if not self.acquire(wait, request_yield=request_yield):
            raise DataFolderInUse(self.data_dir, read_holder(self.data_dir))
        return self

    def _write_info(self) -> None:
        try:
            tmp = self.info_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"role": self.role, "pid": os.getpid(), "since": _now().isoformat()}), "utf-8")
            os.replace(tmp, self.info_path)
        except OSError:
            pass

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)

    def __enter__(self) -> "StoreLock":
        return self.require()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def read_holder(data_dir: Path | str) -> Optional[dict[str, Any]]:
    try:
        return json.loads((Path(data_dir) / LOCK_INFO).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def yield_requested(data_dir: Path | str) -> bool:
    return (Path(data_dir) / YIELD_NAME).exists()


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


# ============================================================================================ integrity
def quick_check(db_path: Path | str, *, full: bool = False) -> tuple[bool, str]:
    """(ok, detail). A missing file is fine (a new store is created). Never raises."""
    p = Path(db_path)
    if not p.exists():
        return True, "absent"
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=rw", uri=True, timeout=30)
    except sqlite3.Error as e:
        return False, f"{type(e).__name__}: {e}"
    try:
        rows = conn.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check").fetchall()
        msgs = [str(r[0]) for r in rows]
        if msgs != ["ok"]:
            return False, "; ".join(msgs[:5])[:1000]
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True, "ok"
    except sqlite3.Error as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def is_damage_error(exc: BaseException) -> bool:
    """True for SQLite errors that mean the file itself is damaged (not a busy/locked/constraint error)."""
    if not isinstance(exc, sqlite3.DatabaseError) or isinstance(exc, (sqlite3.IntegrityError, sqlite3.ProgrammingError)):
        return False
    msg = str(exc).lower()
    return "malformed" in msg or "not a database" in msg or "disk image" in msg or "corrupt" in msg


def _project_count(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT count(*) FROM cases WHERE coalesce(json_extract(settings,'$.kind'),'') != 're_session'").fetchone()
        return int(row[0])
    except sqlite3.Error:
        try:
            return int(conn.execute("SELECT count(*) FROM cases").fetchone()[0])
        except sqlite3.Error:
            return None


# ============================================================================================ backups
class BackupFailed(RuntimeError):
    pass


def backups_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / BACKUP_DIR


_backup_lock = threading.Lock()


def backup_store(db_path: Path | str, reason: str = "manual", *, source: Optional[sqlite3.Connection] = None,
                 now: Optional[datetime] = None, rotate: bool = True) -> dict[str, Any]:
    """Online-backup copy of the store into ``<data>/backups/``, verified with PRAGMA integrity_check before it is kept.
    ``source`` lets the caller back up through its own open connection (used before a schema migration)."""
    db_path = Path(db_path)
    if source is None and not db_path.exists():
        raise BackupFailed(f"no database at {db_path}")
    reason = re.sub(r"[^a-z0-9_-]", "-", reason.lower())[:32] or "manual"
    bdir = backups_dir(db_path.parent)
    bdir.mkdir(parents=True, exist_ok=True)
    taken = now or _now()
    with _backup_lock:
        base = f"studio-{_stamp(taken)}-{reason}"
        name, n = f"{base}.sqlite3", 1
        while (bdir / name).exists():
            name, n = f"{base}-{n}.sqlite3", n + 1
        tmp = bdir / f".tmp-{name}"
        _unlink(tmp)
        src = source or sqlite3.connect(f"file:{db_path.as_posix()}?mode=rw", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        except sqlite3.Error as e:
            _unlink(tmp)
            raise BackupFailed(f"backup of {db_path.name} failed: {type(e).__name__}: {e}") from e
        finally:
            if source is None:
                src.close()
        chk = sqlite3.connect(str(tmp))
        try:
            rows = [str(r[0]) for r in chk.execute("PRAGMA integrity_check").fetchall()]
            projects = _project_count(chk)
        except sqlite3.Error as e:
            rows, projects = [f"{type(e).__name__}: {e}"], None
        finally:
            chk.close()
        if rows != ["ok"]:
            _unlink(tmp)
            raise BackupFailed(f"backup copy failed its integrity check ({'; '.join(rows[:3])}); not kept")
        final = bdir / name
        os.replace(tmp, final)
        meta = {"name": name, "taken_at": taken.isoformat(), "reason": reason, "projects": projects,
                "size": final.stat().st_size, "integrity": "ok"}
        try:
            (bdir / (name + ".json")).write_text(json.dumps(meta), "utf-8")
        except OSError:
            pass
        if rotate:
            rotate_backups(bdir, now=taken)
    return {**meta, "path": str(final)}


def _parse_taken(name: str) -> Optional[datetime]:
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_backups(data_dir: Path | str) -> list[dict[str, Any]]:
    """Kept backups, newest first: name, path, taken_at, reason, projects, size."""
    bdir = backups_dir(data_dir)
    out: list[dict[str, Any]] = []
    if not bdir.is_dir():
        return out
    for p in bdir.glob("studio-*.sqlite3"):
        taken = _parse_taken(p.name)
        if taken is None:
            continue
        meta: dict[str, Any] = {}
        try:
            meta = json.loads((bdir / (p.name + ".json")).read_text("utf-8"))
        except (OSError, ValueError):
            pass
        m = _NAME_RE.match(p.name)
        out.append({"name": p.name, "path": str(p), "taken_at": taken.isoformat(), "reason": meta.get("reason") or (m.group(2) if m else ""),
                    "projects": meta.get("projects"), "size": p.stat().st_size, "integrity": meta.get("integrity", "ok")})
    out.sort(key=lambda b: (b["taken_at"], b["name"]), reverse=True)
    return out


def rotate_backups(bdir: Path | str, *, keep_last: int = KEEP_LAST, keep_days: int = KEEP_DAYS,
                   now: Optional[datetime] = None) -> list[str]:
    """Keep the newest ``keep_last`` backups plus the newest backup of each of the last ``keep_days`` days. Returns the
    names removed."""
    bdir = Path(bdir)
    now = now or _now()
    items = sorted(((t, p) for p in bdir.glob("studio-*.sqlite3") if (t := _parse_taken(p.name)) is not None),
                   key=lambda x: (x[0], x[1].name), reverse=True)
    keep = {p.name for _, p in items[:keep_last]}
    cutoff = (now - timedelta(days=keep_days - 1)).date()
    seen_days: set[Any] = set()
    for t, p in items:
        d = t.date()
        if d >= cutoff and d not in seen_days:
            seen_days.add(d)
            keep.add(p.name)
    removed = []
    for _, p in items:
        if p.name not in keep:
            _unlink(p)
            _unlink(bdir / (p.name + ".json"))
            removed.append(p.name)
    for stale in bdir.glob(".tmp-studio-*.sqlite3"):  # interrupted backup
        _unlink(stale)
    return removed


class PeriodicBackup:
    """Background thread: one verified backup every ``interval`` seconds while the controller runs."""

    def __init__(self, db_path: Path | str, interval: float = PERIODIC_SECONDS, on_result: Optional[Callable[[dict[str, Any]], None]] = None):
        self.db_path, self.interval, self.on_result = Path(db_path), interval, on_result
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="store-backup", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                res: dict[str, Any] = backup_store(self.db_path, "periodic")
            except Exception as e:  # noqa: BLE001 - reported, never fatal
                res = {"error": f"{type(e).__name__}: {e}"}
            if self.on_result:
                try:
                    self.on_result(res)
                except Exception:  # noqa: BLE001
                    pass

    def stop(self) -> None:
        self._stop.set()


# ============================================================================================ startup check + recovery
class StoreDamaged(RuntimeError):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"the project database is damaged: {detail}")


def write_recovery_file(data_dir: Path | str, detail: str) -> dict[str, Any]:
    data_dir = Path(data_dir)
    info = {"status": "damaged", "detail": detail[:2000], "checked_at": _now().isoformat(), "db_path": str(data_dir / DB_NAME),
            "exit_code": EXIT_STORE_DAMAGED,
            "backups": [{k: b[k] for k in ("name", "taken_at", "projects", "reason")} for b in list_backups(data_dir)]}
    tmp = data_dir / (RECOVERY_FILE + ".tmp")
    tmp.write_text(json.dumps(info, indent=2), "utf-8")
    os.replace(tmp, data_dir / RECOVERY_FILE)
    return info


def read_recovery_file(data_dir: Path | str) -> Optional[dict[str, Any]]:
    try:
        return json.loads((Path(data_dir) / RECOVERY_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def check_store(data_dir: Path | str) -> None:
    """Raise StoreDamaged (after writing store-recovery.json) when the store fails PRAGMA quick_check."""
    data_dir = Path(data_dir)
    ok, detail = quick_check(data_dir / DB_NAME)
    if not ok:
        write_recovery_file(data_dir, detail)
        raise StoreDamaged(detail)
    _unlink(data_dir / RECOVERY_FILE)


def _move_damaged(data_dir: Path, stamp: str) -> list[str]:
    ddir = data_dir / DAMAGED_DIR
    ddir.mkdir(parents=True, exist_ok=True)
    moved = []
    for suffix in ("", "-wal", "-shm"):
        src = data_dir / (DB_NAME + suffix)
        if src.exists():
            dst = ddir / f"studio-{stamp}.sqlite3{suffix}"
            n = 1
            while dst.exists():
                dst = ddir / f"studio-{stamp}-{n}.sqlite3{suffix}"
                n += 1
            os.replace(src, dst)
            moved.append(str(dst))
    return moved


def recover_store(data_dir: Path | str, mode: str, backup_name: Optional[str] = None) -> dict[str, Any]:
    """``mode`` = "restore" (the named backup, default the newest) or "empty". The current store files are moved to
    ``<data>/damaged/`` first - never deleted. Raises ValueError/BackupFailed when the request cannot be honoured; the
    current files are then left untouched."""
    data_dir = Path(data_dir)
    if mode not in ("restore", "empty"):
        raise ValueError("mode must be restore or empty")
    src: Optional[Path] = None
    if mode == "restore":
        backups = list_backups(data_dir)
        if not backups:
            raise ValueError("there is no backup to restore")
        if backup_name:
            if "/" in backup_name or "\\" in backup_name or not _NAME_RE.match(backup_name):
                raise ValueError(f"not a backup name: {backup_name!r}")
            match = [b for b in backups if b["name"] == backup_name]
            if not match:
                raise ValueError(f"backup {backup_name} not found")
            chosen = match[0]
        else:
            chosen = backups[0]
        src = Path(chosen["path"])
        ok, detail = quick_check(src, full=True)
        if not ok:
            raise BackupFailed(f"backup {src.name} failed its integrity check: {detail}")
    stamp = _stamp(_now())
    moved = _move_damaged(data_dir, stamp)
    restored = None
    if src is not None:
        tmp = data_dir / (DB_NAME + ".restore-tmp")
        _unlink(tmp)
        s, d = sqlite3.connect(str(src)), sqlite3.connect(str(tmp))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        os.replace(tmp, data_dir / DB_NAME)
        restored = src.name
    _unlink(data_dir / RECOVERY_FILE)
    entry = {"at": _now().isoformat(), "mode": mode, "restored": restored, "damaged_files": moved}
    try:
        with open(data_dir / "recovery-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return entry


def parse_recover_env(value: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    """``REBUILD_STUDIO_RECOVER``: "empty" | "restore" | "restore:<backup name>" -> (mode, name)."""
    if not value:
        return None
    mode, _, name = value.partition(":")
    if mode not in ("restore", "empty"):
        raise ValueError(f"{ENV_RECOVER} must be 'empty' or 'restore[:<backup name>]', got {value!r}")
    return mode, (name or None)
