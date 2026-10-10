"""Dependency health (R10): is everything each project needs checked, installed and running?

One service answers, for every pinned tool in docs/dependency-lock.json and every runtime service a project relies on:
installed / version / hash ok / smoke-runs / running / needed by which project. It also checks host prerequisites
(WebView2 runtime, free disk space, write access to the tools folder, long-path support and, on demand, whether each
pinned download host can be reached).

Which tools a project needs comes from ONE table that lives next to the lock: docs/dependency-needs.json (mirrored to
rebuild_controller/data/). A project's needs are derived from its profile (detected cheaply before the first run, or taken
from the inventory once it exists), its target, its AI policy (local AI -> the local AI server must be reachable) and
its comparator kind (web comparisons -> Node.js + the browser test library + a Chromium-based browser).

Installing always goes through ``tool_setup.ToolSetup`` (the one hash-verified downloader). ``InstallQueue`` only orders
the work (dependencies first), runs one tool at a time, keeps per-item errors with next actions, survives a restart
(``<tools>/.install-queue.json``) and can be retried. Nothing is installed unless the user clicked, or turned on
"install what projects need automatically" (default off, asked once). Software outside the tools folder is never
installed; an already-installed Ollama is started only when the user presses "Start Ollama".
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .ids import now_iso
from .tool_setup import FRIENDLY, ToolSetup, ToolSetupError, _rmtree, find_lock_path

log = logging.getLogger(__name__)

NEEDS_FILE = "dependency-needs.json"
QUEUE_FILE = ".install-queue.json"
SETTINGS_FILE = "dependencies.json"
SATISFIED = ("installed", "update_available", "external")
QUICK_SCAN_FILES = 2500
REPORT_TTL_S = 3.0


# ====================================================================================== the needs table
def find_needs_path(lock_path: Path | str | None = None) -> Path | None:
    """Next to the lock in use (installed app / repo docs), else the packaged copy."""
    cands: list[Path] = []
    if lock_path:
        cands.append(Path(lock_path).parent / NEEDS_FILE)
    lp = find_lock_path()
    if lp:
        cands.append(lp.parent / NEEDS_FILE)
    cands.append(Path(__file__).resolve().parent / "data" / NEEDS_FILE)
    for c in cands:
        if c.is_file():
            return c
    return None


def load_needs(lock_path: Path | str | None = None) -> dict[str, Any]:
    p = find_needs_path(lock_path)
    if p is None:
        raise ToolSetupError("needs_missing", "The list of what each project needs (dependency-needs.json) was not found.",
                             next_action="Reinstall Rebuild Studio; the file ships with the application.", status=500)
    return json.loads(p.read_text(encoding="utf-8"))


def lock_hash_problems(lock: dict[str, Any]) -> list[str]:
    """Packaged-build gate: every installable tool must carry a sha256 (the build script runs the same rule)."""
    out = []
    for name, t in (lock.get("tools") or {}).items():
        art = (t or {}).get("artifact") if isinstance(t, dict) else None
        if not art:
            continue
        sha = art.get("sha256")
        if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha)):
            out.append(f"{name}: artifact.sha256 is missing or not a sha256")
    return out


# ====================================================================================== cheap profile detection
def quick_profile(root: Path | str, max_files: int = QUICK_SCAN_FILES) -> str:
    """Primary profile of an installation folder from file headers only (bounded; no hashing, no tools)."""
    from .backends.detect import sniff, summarize_profile
    root = Path(root)
    dets = []
    try:
        if root.is_file():
            return summarize_profile([(root.name, sniff(root))], root.parent)["primary"]
        n = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
            for fn in sorted(filenames):
                if n >= max_files:
                    break
                p = Path(dirpath) / fn
                if p.is_symlink():
                    continue
                n += 1
                try:
                    dets.append((p.relative_to(root).as_posix(), sniff(p)))
                except Exception:  # noqa: BLE001 - one unreadable file never fails the check
                    continue
            if n >= max_files:
                break
        return summarize_profile(dets, root)["primary"]
    except Exception:  # noqa: BLE001
        return "unknown"


# ====================================================================================== host / service probes
def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_ollama() -> Path | None:
    """An Ollama the user already installed (never installed by Rebuild Studio)."""
    cands = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(Path(la) / "Programs" / "Ollama" / ("ollama.exe" if os.name == "nt" else "ollama"))
    w = shutil.which("ollama")
    if w:
        cands.append(Path(w))
    for c in cands:
        if c.is_file():
            return c
    return None


def _spawn_detached(argv: list[str]) -> None:
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                     close_fds=True, **kw)


def _reg_value(path: str, value: str) -> Any:
    if os.name != "nt":
        return None
    import winreg
    hive_name, _, sub = path.partition("\\")
    hive = {"HKLM:": winreg.HKEY_LOCAL_MACHINE, "HKCU:": winreg.HKEY_CURRENT_USER}.get(hive_name.upper())
    if hive is None:
        return None
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as k:
                return winreg.QueryValueEx(k, value)[0]
        except OSError:
            continue
    return None


def _free_bytes(path: Path) -> int | None:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _mb(n: int | float | None) -> str:
    n = float(n or 0)
    return f"{n / 1_000_000_000:.1f} GB" if n >= 1_000_000_000 else f"{max(1, round(n / 1_000_000))} MB"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# ====================================================================================== install queue
class InstallQueue:
    """Installs a list of tools one after the other through ToolSetup (dependencies first), with one combined progress."""

    def __init__(self, setup: ToolSetup, events: Any = None, *, state_path: Path | None = None, poll_s: float = 0.25):
        self.setup = setup
        self.events = events
        self.state_path = state_path
        self.poll_s = poll_s
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"id": None, "state": "idle", "items": [], "reason": None, "started_at": None, "finished_at": None}
        self._load()

    # -- persistence (an interrupted queue is reported, and resumed when the user or auto-install says so) ------------
    def _load(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if doc.get("state") == "running":           # the app stopped while installing
            doc["state"] = "interrupted"
            for it in doc.get("items") or []:
                if it.get("status") in ("queued", "installing"):
                    it["status"], it["message"] = "interrupted", "Stopped when Rebuild Studio closed."
        self._state = doc

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:  # never break an install over a progress file
            log.info("could not save install queue: %s", e)

    def _emit(self) -> None:
        if self.events is None:
            return
        try:
            snap = self.snapshot()
            self.events.emit("dependencies.queue", {"state": snap["state"], "done": snap["done"], "total": snap["total"],
                                                   "current": snap["current"]})
        except Exception:  # noqa: BLE001
            pass

    # -- ordering ------------------------------------------------------------------------------------------------------
    def order(self, names: Iterable[str], *, repair: Iterable[str] = ()) -> list[str]:
        """Topological order with missing dependencies added in front (e.g. .NET runtime before ILSpy)."""
        repair = set(repair)
        out: list[str] = []

        def visit(n: str, stack: tuple[str, ...]) -> None:
            if n in out or n in stack:
                return
            for r in self.setup._requires(n):
                if not self.setup._healthy(r) or r in repair:
                    visit(r, stack + (n,))
            out.append(n)

        for n in names:
            self.setup._tool(n)          # unknown names fail fast (404 with a plain message)
            visit(n, ())
        return out

    # -- public ------------------------------------------------------------------------------------------------------
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, names: Iterable[str], *, repair: Iterable[str] = (), reason: str = "") -> dict[str, Any]:
        names = [n for n in dict.fromkeys(names)]
        repair = set(repair)
        if not names:
            raise ToolSetupError("nothing_to_install", "Everything that was asked for is already installed.", status=409,
                                 next_action="Nothing to do.")
        with self._lock:
            if self.busy() or self.setup._busy():
                raise ToolSetupError("busy", "Another install is running. Wait for it to finish or cancel it.", status=409,
                                     next_action="Wait for the running install, or press Cancel on it.")
            order = self.order(names, repair=repair)
            items = []
            for n in order:
                st = self.setup.status(n)
                art_size = int(st.get("size_bytes") or 0) + int((st.get("footprint") or {}).get("installer_download_bytes") or 0)
                items.append({"name": n, "title": st["title"], "status": "queued", "repair": n in repair, "requested": n in names,
                              "bytes_total": art_size, "bytes_done": 0, "phase": None, "message": "Waiting", "error": None})
            self._state = {"id": uuid.uuid4().hex[:12], "state": "running", "items": items, "reason": reason or None,
                           "started_at": now_iso(), "finished_at": None}
            self._cancel = threading.Event()
            self._save()
            th = threading.Thread(target=self._run, name="dependency-install-queue", daemon=True)
            self._thread = th
            th.start()
        self._emit()
        return self.snapshot()

    def retry(self) -> dict[str, Any]:
        with self._lock:
            items = [it for it in self._state.get("items") or [] if it["status"] not in ("done",)]
            names = [it["name"] for it in items]
            repair = [it["name"] for it in items if it.get("repair")]
        if not names:
            raise ToolSetupError("nothing_to_retry", "Nothing failed, so there is nothing to retry.", status=409, next_action="Nothing to do.")
        return self.start(names, repair=repair, reason=self._state.get("reason") or "retry")

    resume = retry

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if not self.busy():
                raise ToolSetupError("not_running", "No install is running.", status=409, next_action="Nothing to cancel.")
            self._cancel.set()
            cur = next((it["name"] for it in self._state["items"] if it["status"] == "installing"), None)
        if cur:
            try:
                self.setup.cancel(cur)
            except ToolSetupError:
                pass
        return self.snapshot()

    def join(self, timeout: float | None = None) -> bool:
        th = self._thread
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = json.loads(json.dumps(self._state))
        items = s.get("items") or []
        total = sum(int(it.get("bytes_total") or 0) for it in items)
        done = sum(int(it.get("bytes_total") or 0) if it["status"] == "done" else int(it.get("bytes_done") or 0) for it in items)
        cur = next((it for it in items if it["status"] == "installing"), None)
        s.update({
            "total": len(items), "done": sum(1 for it in items if it["status"] == "done"),
            "failed": [it["name"] for it in items if it["status"] in ("failed", "skipped")],
            "bytes_total": total, "bytes_done": min(done, total) if total else done,
            "percent": round(100 * done / total, 1) if total else None,
            "current": cur["name"] if cur else None, "running": s.get("state") == "running" and self.busy(),
        })
        return s

    # -- worker ------------------------------------------------------------------------------------------------------
    def _set(self, it: dict[str, Any], save: bool = True, **kw: Any) -> None:
        with self._lock:
            it.update(kw)
            if save:
                self._save()
        if save:
            self._emit()

    def _run(self) -> None:
        items = self._state["items"]
        failed: set[str] = set()
        try:
            for it in items:
                n = it["name"]
                if self._cancel.is_set():
                    self._set(it, status="cancelled", message="Cancelled before it started. Nothing was installed.")
                    continue
                bad = [r for r in self.setup._requires(n) if r in failed]
                if bad:
                    dep = FRIENDLY.get(bad[0], (bad[0],))[0]
                    self._set(it, status="skipped", message=f"Not installed because {dep} failed.",
                              error={"code": "dependency_failed", "message": f"{it['title']} needs {dep}, which could not be installed.",
                                     "affected": bad[0], "next_action": f"Fix {dep} first (see its error), then press Retry.", "retryable": True, "url": None})
                    failed.add(n)
                    continue
                if not it.get("repair") and self.setup._disk_status(n) == "installed":
                    self._set(it, status="done", message="Already installed", bytes_done=it["bytes_total"])
                    continue
                self._set(it, status="installing", message="Starting", error=None)
                try:
                    self.setup.install(n, force=bool(it.get("repair")))
                except ToolSetupError as e:
                    self._set(it, status="failed", message=e.message, error=e.to_dict())
                    failed.add(n)
                    continue
                while not self.setup.join(self.poll_s):
                    job = (self.setup.status(n).get("job") or {})
                    self._set(it, save=False, phase=job.get("phase"), message=job.get("message") or it["message"],
                              bytes_done=min(int(job.get("bytes_done") or 0), int(it["bytes_total"] or 0) or int(job.get("bytes_done") or 0)))
                    if self._cancel.is_set():
                        try:
                            self.setup.cancel(n)
                        except ToolSetupError:
                            pass
                st = self.setup.status(n)
                job = st.get("job") or {}
                if job.get("phase") == "cancelled" or (self._cancel.is_set() and st["disk_status"] not in ("installed",)):
                    self._set(it, status="cancelled", message="Cancelled. Nothing was installed.", phase="cancelled")
                elif job.get("error") or st["disk_status"] not in ("installed", "update_available"):
                    err = job.get("error") or {"code": "not_installed", "message": f"{it['title']} is still not installed.", "affected": n,
                                               "next_action": "Press Retry.", "retryable": True, "url": None}
                    self._set(it, status="failed", message=err.get("message"), error=err, phase="failed")
                    failed.add(n)
                else:
                    self._set(it, status="done", message="Installed", phase="done", bytes_done=it["bytes_total"])
        except Exception as e:  # noqa: BLE001
            log.exception("install queue failed")
            for it in items:
                if it["status"] in ("queued", "installing"):
                    self._set(it, status="failed", message=f"Unexpected error: {type(e).__name__}: {e}",
                              error={"code": "internal", "message": f"Unexpected error: {type(e).__name__}: {e}", "affected": it["name"],
                                     "next_action": "Press Retry; if it repeats, check the logs.", "retryable": True, "url": None})
        with self._lock:
            sts = {it["status"] for it in items}
            self._state["state"] = ("cancelled" if self._cancel.is_set() and "failed" not in sts
                                    else "failed" if sts & {"failed", "skipped"} else "done")
            self._state["finished_at"] = now_iso()
            self._save()
        self._emit()


# ====================================================================================== the health service
class DependencyHealth:
    def __init__(self, studio: Any, setup: ToolSetup | None = None, *, alt_probes: dict[str, Callable[[], str | None]] | None = None,
                 service_probes: dict[str, Callable[..., dict[str, Any]]] | None = None):
        self.st = studio
        self.setup = setup or ToolSetup(studio.settings, getattr(studio, "events", None))
        self.events = getattr(studio, "events", None)
        self.alt_probes = self._default_alt_probes() if alt_probes is None else alt_probes
        self.service_probes = service_probes or {}
        self.queue = InstallQueue(self.setup, self.events, state_path=Path(self.setup.tools_dir) / QUEUE_FILE)
        self.settings_path = Path(studio.settings.data_dir) / SETTINGS_FILE
        self._lock = threading.RLock()
        self._profiles: dict[tuple[str, str], str] = {}
        self._smoke: dict[str, dict[str, Any]] = {}
        self._hosts: dict[str, Any] | None = None
        self._report: tuple[float, dict[str, Any]] | None = None
        self._needs: dict[str, Any] | None = None

    # -- settings: "install what projects need automatically" (default off, asked once) --------------------------------
    def get_settings(self) -> dict[str, Any]:
        doc = {"auto_install": False, "asked": False}
        try:
            doc.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return {"auto_install": bool(doc.get("auto_install")), "asked": bool(doc.get("asked")), "updated_at": doc.get("updated_at")}

    def put_settings(self, *, auto_install: bool) -> dict[str, Any]:
        doc = {"auto_install": bool(auto_install), "asked": True, "updated_at": now_iso()}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        out = self.get_settings()
        out["install"] = None
        if auto_install:                # the user just said yes: install what is needed now
            out["install"] = self.auto_install("auto-install turned on")
        return out

    # -- the table -----------------------------------------------------------------------------------------------------
    def needs(self) -> dict[str, Any]:
        if self._needs is None:
            self._needs = load_needs(self.setup.lock_path)
        return self._needs

    def _known(self, names: Iterable[str]) -> list[str]:
        tools = self.setup._tools()
        return [n for n in names if n in tools]

    def _default_alt_probes(self) -> dict[str, Callable[[], str | None]]:
        """Tools found outside Rebuild Studio's tools folder that the pipeline would really use (e.g. cargo on PATH)."""
        def rizin() -> str | None:
            from .backends.rizin_worker import find_rizin
            t = find_rizin(self.st.settings)
            return str(t.exe) if t else None

        def rust() -> str | None:
            return shutil.which("cargo")

        def node() -> str | None:
            return shutil.which("node")

        def dotnet_sdk() -> str | None:      # a .NET 8+ SDK on PATH builds C# rebuilds too (R4)
            from .builders.dotnet import system_sdk
            s = system_sdk()
            return f"{s['dotnet']} (SDK {s['sdk']})" if s else None

        def jdk() -> str | None:             # a javac on JAVA_HOME/PATH compiles Java rebuilds (R4); Ghidra still asks for the private JDK 21
            from .builders.java import system_jdk
            j = system_jdk()
            return j["javac"] if j else None
        return {"rizin": rizin, "rust": rust, "node": node, "dotnet-sdk": dotnet_sdk, "temurin-jdk21": jdk}

    # -- per project ---------------------------------------------------------------------------------------------------
    def profile_of(self, case: dict[str, Any]) -> str:
        cid = case["case_id"]
        try:
            evs = self.st.cases.list_evidence(cid, kind="inventory")
            if evs:
                prof = ((evs[-1].get("meta") or {}).get("profile"))
                if not prof:
                    prof = ((self.st.cases.evidence_body(evs[-1]["evidence_id"]) or {}).get("profile") or {}).get("primary")
                if prof:
                    return str(prof)
        except Exception:  # noqa: BLE001
            pass
        key = (cid, str(case.get("source_root")))
        with self._lock:
            if key in self._profiles:
                return self._profiles[key]
        prof = quick_profile(case.get("source_root") or "")
        with self._lock:
            self._profiles[key] = prof
        return prof

    def _ai_need(self, case: dict[str, Any]) -> dict[str, Any] | None:
        pol = case.get("ai_policy") or {}
        if (pol.get("mode") or "no_ai") == "no_ai" or getattr(self.st, "connections", None) is None:
            return None
        try:
            from .implement import LoopPolicy, route_status
            rs = route_status(self.st, LoopPolicy.from_case(case))
        except Exception:  # noqa: BLE001 - AI routing belongs to the AI views; never fail the health check over it
            return None
        route = rs.get("route") or []
        local = [e for e in route if e.get("locality") == "local"]
        if not local:
            return None
        endpoints = []
        for e in local:
            try:
                conn = self.st.connections.get(e["connection_id"])
            except Exception:  # noqa: BLE001
                continue
            endpoints.append({"connection_id": e["connection_id"], "label": e.get("label") or conn.get("label"), "model": e.get("model"),
                              "endpoint": conn.get("endpoint") or "", "server": (conn.get("limits") or {}).get("server")})
        return {"required": len(local) == len(route), "endpoints": endpoints}

    def case_needs(self, case: dict[str, Any]) -> dict[str, Any]:
        tbl = self.needs()
        profile = self.profile_of(case)
        target = case.get("target_language") or "rust"
        if target == "auto":
            at = tbl.get("auto_target") or {}
            target = next((t for t, profs in at.items() if t != "default" and isinstance(profs, list) and profile in profs), at.get("default", "rust"))
        lp = case.get("launch_profile") or {}
        comparator = lp.get("kind") or ("web" if case.get("target_language") == "web" else "cli")
        compares = bool(lp.get("execute_original") or lp.get("allow_original_execution") or lp.get("baseline_file") or lp.get("scenarios"))
        required: dict[str, list[str]] = {}
        optional: dict[str, list[str]] = {}
        services: dict[str, dict[str, Any]] = {}

        def add(bucket: dict[str, list[str]], names: Iterable[str], why: str) -> None:
            for n in self._known(names):
                bucket.setdefault(n, []).append(why)

        p = (tbl.get("profiles") or {}).get(profile) or (tbl.get("profiles") or {}).get("unknown") or {}
        ptitle = p.get("title") or profile
        add(required, p.get("required") or [], f"to analyze {ptitle}")
        add(optional, p.get("optional") or [], f"optional for {ptitle}")
        for s in p.get("services") or []:
            services[s] = {"required": True, "why": f"to analyze {ptitle}"}
        t = (tbl.get("targets") or {}).get(target) or {}
        add(required, t.get("required") or [], f"to build the {t.get('title') or target} rebuild")
        add(optional, t.get("optional") or [], f"optional for the {t.get('title') or target} rebuild")
        c = (tbl.get("comparators") or {}).get(comparator) or {}
        add(required if compares else optional, c.get("required") or [], f"for {c.get('title') or comparator}")
        for s in c.get("services") or []:
            services[s] = {"required": compares, "why": f"for {c.get('title') or comparator}"}
        ai = self._ai_need(case)
        if ai:
            services["local_ai_server"] = {"required": ai["required"], "why": "this project uses AI on this PC", "endpoints": ai["endpoints"]}
        for n in list(optional):
            if n in required:
                optional.pop(n)
        return {"profile": profile, "profile_title": ptitle, "target": target, "comparator": comparator, "compares": compares,
                "ai": "local" if ai else ((case.get("ai_policy") or {}).get("mode") or "no_ai"),
                "required": required, "optional": optional, "services": services}

    def _projects(self) -> list[dict[str, Any]]:
        try:
            from .re_workbench import is_re_case
            return [c for c in self.st.cases.list_cases() if not is_re_case(c)]
        except Exception:  # noqa: BLE001
            return []

    # -- tools ---------------------------------------------------------------------------------------------------------
    def tool_state(self, name: str) -> dict[str, Any]:
        s = self.setup.status(name)
        state = s["status"]
        external = None
        if s["disk_status"] in ("not_installed", "blocked_unverified") and state != "installing" and name in self.alt_probes:
            try:
                external = self.alt_probes[name]()
            except Exception:  # noqa: BLE001
                external = None
            if external:
                state = "external"
        smoke = self._smoke.get(name)
        if smoke and not smoke["ok"] and state in ("installed", "update_available"):
            state = "broken"
        fp = s.get("footprint") or {}
        download = int(s.get("size_bytes") or 0) + int(fp.get("installer_download_bytes") or 0)
        disk = int(fp.get("disk_bytes") or 0) or int((s.get("size_bytes") or 0) * 3)
        return {"name": name, "title": s["title"], "purpose": s["purpose"], "optional": s["optional"], "state": state,
                "status": s["status"], "disk_status": s["disk_status"], "satisfied": state in SATISFIED,
                "installable": not s.get("blocked_reason") and state != "installing",
                "version": s["version"], "installed_version": s["installed_version"],
                "hash_ok": None if s["disk_status"] in ("not_installed", "blocked_unverified") else s["disk_status"] != "corrupt",
                "smoke": smoke, "external_path": external, "requires": s["requires"], "blocked_reason": s.get("blocked_reason"),
                "download_bytes": download, "disk_bytes": disk, "install_path": s["install_path"], "job": s.get("job")}

    def _closure(self, names: Iterable[str], states: dict[str, dict[str, Any]]) -> list[str]:
        """What has to be installed for ``names``: the unsatisfied ones plus their missing dependencies, dependencies first.
        A dependency counts as present only inside the tools folder (ToolSetup installs a private copy even if one is on PATH)."""
        out: list[str] = []

        def visit(n: str, top: bool) -> None:
            st = states[n]
            need = (not st["satisfied"] or st["state"] == "broken") if top else st["state"] not in ("installed", "update_available")
            if n in out or not need:
                return
            for r in self.setup._requires(n):
                visit(r, False)
            out.append(n)
        for n in names:
            visit(n, True)
        return out

    def smoke_check(self, names: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
        """Run each installed tool's version check on the installed copy (same check as during install)."""
        names = list(names) if names is not None else list(self.setup._tools())

        def one(n: str) -> tuple[str, dict[str, Any] | None]:
            if self.setup._disk_status(n) not in ("installed", "update_available"):
                return n, None
            layout = self.setup._tool(n).get("layout") or {}
            try:
                self.setup._version_check(n, self.setup._install_dir(n), layout)
                return n, {"ok": True, "message": "Starts and answers its version check.", "at": now_iso()}
            except ToolSetupError as e:
                return n, {"ok": False, "message": e.message, "at": now_iso(), "code": e.code}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dep-smoke") as pool:
            for n, res in pool.map(one, names):
                with self._lock:
                    if res is None:
                        self._smoke.pop(n, None)
                    else:
                        self._smoke[n] = res
        return dict(self._smoke)

    # -- services ------------------------------------------------------------------------------------------------------
    def _service(self, name: str, need: dict[str, Any] | None, tools: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if name in self.service_probes:
            return self.service_probes[name](self, need)
        if name == "rizin_worker":
            return self._svc_rizin(tools)
        if name == "browser":
            return self._svc_browser()
        if name == "local_ai_server":
            return self._svc_local_ai(need)
        return {"name": name, "state": "unknown", "sentence": f"{name}: not checked.", "action": None}

    def _svc_rizin(self, tools: dict[str, dict[str, Any]]) -> dict[str, Any]:
        base = {"name": "rizin_worker", "title": "Native analysis engine (rizin)"}
        try:
            b = self.st.registry.get("rizin")
        except Exception:  # noqa: BLE001
            b = None
        if b is None or getattr(b, "error", None):
            why = getattr(b, "error", None) or "it is not registered"
            return {**base, "state": "error", "sentence": f"The native analysis engine failed to load ({why}).",
                    "action": None, "next_action": "Reinstall Rebuild Studio; if it repeats, report the message."}
        rz = tools.get("rizin")
        if rz and not rz["satisfied"]:
            return {**base, "state": "missing", "sentence": "Rizin is not installed, so native programs cannot be analyzed.",
                    "action": {"kind": "install", "items": ["rizin"], "label": "Install Rizin"}}
        sessions = 0
        try:
            sessions = len([s for s in b.pool.sessions() if getattr(s, "_pipe", None) is not None])
        except Exception:  # noqa: BLE001
            pass
        return {**base, "state": "ok", "sentence": f"Ready ({sessions} analysis process{'es' if sessions != 1 else ''} open).", "action": None}

    def _svc_browser(self) -> dict[str, Any]:
        base = {"name": "browser", "title": "Browser for comparisons (Microsoft Edge)"}
        try:
            from .comparators.web import chromium_path
            path = chromium_path()
        except Exception:  # noqa: BLE001
            path = None
        if path:
            return {**base, "state": "ok", "sentence": f"Found {Path(path).name}.", "path": path, "action": None}
        return {**base, "state": "down", "sentence": "No Chromium-based browser (Microsoft Edge) was found, so web comparisons cannot run.",
                "action": None, "next_action": "Install or repair Microsoft Edge from Windows Settings > Apps, then press Recheck."}

    def _svc_local_ai(self, need: dict[str, Any] | None) -> dict[str, Any]:
        base = {"name": "local_ai_server", "title": "Local AI server"}
        eps = (need or {}).get("endpoints") or []
        if not eps:
            eps = [{"endpoint": "http://127.0.0.1:11434/v1", "server": "ollama", "label": "Ollama (this PC)"}]
        down = []
        for e in eps:
            u = urlsplit(e.get("endpoint") or "")
            host, port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
            if host not in ("127.0.0.1", "localhost", "::1") and not host.startswith("127."):
                continue
            if not _port_open(host, port):
                kind = e.get("server") or ("ollama" if port == 11434 else "lmstudio" if port == 1234 else "local")
                down.append({**e, "kind": kind, "port": port})
        if not down:
            return {**base, "state": "ok", "sentence": "The local AI server answers.", "action": None}
        d = down[0]
        if d["kind"] == "ollama":
            exe = find_ollama()
            if exe:
                return {**base, "state": "down", "sentence": "Ollama is installed but not running.", "ollama_path": str(exe),
                        "action": {"kind": "start_ollama", "label": "Start Ollama"}}
            return {**base, "state": "down", "sentence": "Ollama is not running, and it is not installed on this PC.",
                    "action": {"kind": "link", "label": "Get Ollama", "url": "https://ollama.com/download"},
                    "next_action": "Install Ollama yourself from ollama.com (Rebuild Studio never installs it), then press Recheck, or switch this project to cloud AI."}
        name = "LM Studio" if d["kind"] == "lmstudio" else (d.get("label") or "The local AI server")
        return {**base, "state": "down", "sentence": f"{name} is not answering at {d.get('endpoint')}.", "action": None,
                "next_action": f"Open {name} and start its local server, then press Recheck."}

    def start_ollama(self, *, wait_s: float = 10.0) -> dict[str, Any]:
        """Start an Ollama the user already installed. Only ever called from the user's click."""
        if _port_open("127.0.0.1", 11434):
            return {"started": False, "running": True, "message": "Ollama is already running."}
        exe = find_ollama()
        if exe is None:
            raise ToolSetupError("not_installed", "Ollama is not installed on this PC, so it cannot be started.", status=404,
                                 next_action="Install Ollama yourself from https://ollama.com/download (Rebuild Studio never installs it), then press Recheck.",
                                 url="https://ollama.com/download")
        try:
            _spawn_detached([str(exe), "serve"])
        except OSError as e:
            raise ToolSetupError("start_failed", f"Ollama could not be started ({e}).", status=409,
                                 next_action="Start Ollama from the Start menu, then press Recheck.") from e
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if _port_open("127.0.0.1", 11434):
                self._report = None
                return {"started": True, "running": True, "message": "Ollama started."}
            time.sleep(0.25)
        return {"started": True, "running": False, "message": "Ollama was started but is not answering yet.",
                "next_action": "Wait a few seconds and press Recheck."}

    # -- host ----------------------------------------------------------------------------------------------------------
    def _lock_doc(self) -> dict[str, Any]:
        self.setup._tools()
        return self.setup._lock_doc or {}

    def host_checks(self, *, need_bytes: int = 0, network: bool = False) -> list[dict[str, Any]]:
        out = [self._chk_webview2(), self._chk_disk(need_bytes), self._chk_write(), self._chk_long_paths()]
        if network:
            self._hosts = self._chk_hosts()
        out.append(self._hosts or {"name": "download_hosts", "title": "Download servers", "state": "not_checked",
                                   "sentence": "Not checked yet.", "next_action": "Press 'Check everything' to test the internet connection to each download server."})
        return out

    def _chk_webview2(self) -> dict[str, Any]:
        base = {"name": "webview2", "title": "Microsoft Edge WebView2 runtime"}
        if os.name != "nt":
            return {**base, "state": "not_applicable", "sentence": "Only needed on Windows."}
        spec = ((self._lock_doc().get("system_prerequisites") or {}).get("webview2_evergreen_bootstrapper") or {})
        for key in spec.get("runtime_registry_keys") or []:
            v = _reg_value(key, "pv")
            if v and v != "0.0.0.0":
                return {**base, "state": "ok", "sentence": f"Installed (version {v}).", "version": v}
        return {**base, "state": "warn", "sentence": "The WebView2 runtime was not found in the registry.",
                "next_action": f"If the app window does not open, install it from {spec.get('docs') or 'Microsoft'}."}

    def _chk_disk(self, need: int) -> dict[str, Any]:
        base = {"name": "disk_space", "title": "Free disk space for tools"}
        free = _free_bytes(Path(self.setup.tools_dir))
        if free is None:
            return {**base, "state": "unknown", "sentence": "Free space could not be measured.", "free_bytes": None, "need_bytes": need}
        margin = 200_000_000
        if need and free < need + margin:
            return {**base, "state": "error", "free_bytes": free, "need_bytes": need,
                    "sentence": f"Only {_mb(free)} free; installing what is missing needs about {_mb(need + margin)}.",
                    "next_action": "Free up disk space on this drive (or remove tools you no longer need), then press Recheck."}
        return {**base, "state": "ok", "free_bytes": free, "need_bytes": need, "sentence": f"{_mb(free)} free."}

    def _chk_write(self) -> dict[str, Any]:
        base = {"name": "tools_write", "title": "Write access to the tools folder", "path": str(self.setup.tools_dir)}
        d = Path(self.setup.tools_dir)
        probe = d / f".write-test-{uuid.uuid4().hex[:8]}"
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"ok")
            probe.unlink()
            return {**base, "state": "ok", "sentence": "Rebuild Studio can write to its tools folder."}
        except OSError as e:
            return {**base, "state": "error", "sentence": f"Rebuild Studio cannot write to {d} ({e.strerror or e}).",
                    "next_action": "Check that the folder is not read-only or blocked by security software (Controlled folder access), then press Recheck."}

    def _chk_long_paths(self) -> dict[str, Any]:
        base = {"name": "long_paths", "title": "Long file paths"}
        if os.name != "nt":
            return {**base, "state": "not_applicable", "sentence": "Only relevant on Windows."}
        v = _reg_value("HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem", "LongPathsEnabled")
        if v == 1:
            return {**base, "state": "ok", "sentence": "Long paths are enabled."}
        short = len(str(self.setup.tools_dir)) < 120
        return {**base, "state": "ok" if short else "warn",
                "sentence": "Long paths are off; the tools folder path is short, so this is fine." if short else
                            "Long paths are off and the tools folder path is long; some tools may fail to unpack.",
                "next_action": None if short else "Use a shorter tools folder (REBUILD_STUDIO_TOOLS) or enable long paths in Windows."}

    def _chk_hosts(self) -> dict[str, Any]:
        hosts = sorted({urlsplit(str((t.get("artifact") or {}).get("url") or "")).hostname or "" for t in self.setup._tools().values()} - {""})

        def one(h: str) -> tuple[str, bool]:
            return h, _port_open(h, 443, timeout=4.0)
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(hosts))), thread_name_prefix="dep-net") as pool:
            res = dict(pool.map(one, hosts))
        bad = [h for h, ok in res.items() if not ok]
        base = {"name": "download_hosts", "title": "Download servers", "hosts": res, "checked_at": now_iso()}
        if not bad:
            return {**base, "state": "ok", "sentence": f"All {len(hosts)} download servers can be reached."}
        return {**base, "state": "warn", "sentence": f"Cannot reach {_join(bad)}.",
                "next_action": "Check the internet connection or proxy. Tools can also be installed from a downloaded file on the Tools page."}

    # -- report --------------------------------------------------------------------------------------------------------
    def report(self, *, refresh: bool = False, smoke: bool = False, network: bool = False) -> dict[str, Any]:
        if smoke:
            self.smoke_check()
        if not (refresh or smoke or network):
            with self._lock:
                if self._report and time.monotonic() - self._report[0] < REPORT_TTL_S and not self.queue.busy():
                    return self._report[1]
        names = list(self.setup._tools())
        tools = {n: self.tool_state(n) for n in names}
        tbl = self.needs()
        needed_by: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
        services_need: dict[str, dict[str, Any]] = {}
        blocking_tools: set[str] = set()
        projects = []
        for case in self._projects():
            try:
                cn = self.case_needs(case)
            except Exception:  # noqa: BLE001
                continue
            active = case.get("status") != "completed"
            projects.append({"case_id": case["case_id"], "name": case.get("name"), "profile": cn["profile"], "target": cn["target"],
                             "required": list(cn["required"]), "optional": list(cn["optional"]), "services": list(cn["services"])})
            for n, why in cn["required"].items():
                needed_by[n].append({"kind": "project", "case_id": case["case_id"], "name": case.get("name"), "required": True, "why": why[0]})
                if active:
                    blocking_tools.add(n)
            for n, why in cn["optional"].items():
                needed_by[n].append({"kind": "project", "case_id": case["case_id"], "name": case.get("name"), "required": False, "why": why[0]})
            for s, sn in cn["services"].items():
                agg = services_need.setdefault(s, {"required": False, "projects": [], "endpoints": []})
                agg["required"] = agg["required"] or (sn["required"] and active)
                agg["projects"].append({"case_id": case["case_id"], "name": case.get("name"), "required": sn["required"]})
                agg["endpoints"].extend(sn.get("endpoints") or [])
        core = self._known((tbl.get("core") or {}).get("recommended") or [])
        for n in core:
            needed_by[n].append({"kind": "app", "name": (tbl.get("core") or {}).get("title") or "Rebuild Studio", "required": False,
                                 "why": "recommended for common program types"})
        for n in names:
            tools[n]["needed_by"] = needed_by[n]
            tools[n]["required"] = n in blocking_tools
        missing_required = self._closure(sorted(blocking_tools), tools)
        missing_core = [n for n in self._closure(core, tools) if n not in missing_required and tools[n]["state"] != "broken"]
        services = []
        for s in ("rizin_worker", "local_ai_server", "browser"):
            need = services_need.get(s)
            if need is None and s != "rizin_worker":
                continue
            row = self._service(s, need, tools)
            row.setdefault("title", ((tbl.get("services") or {}).get(s) or {}).get("title") or s)
            row["required"] = bool(need and need["required"])
            row["needed_by"] = (need or {}).get("projects") or []
            services.append(row)
        need_bytes = sum(tools[n]["disk_bytes"] + tools[n]["download_bytes"] for n in missing_required + missing_core)
        host = self.host_checks(need_bytes=need_bytes, network=network)
        queue = self.queue.snapshot()
        out = {"checked_at": now_iso(), "tools_dir": str(self.setup.tools_dir), "tools": [tools[n] for n in names],
               "services": services, "host": host, "projects": projects, "queue": queue, "settings": self.get_settings(),
               "missing_required": missing_required, "missing_recommended": missing_core,
               "install_all": self._install_offer(missing_required + missing_core, tools)}
        out.update(self._overall(out, tools, missing_required, missing_core, services, host, queue))
        with self._lock:
            self._report = (time.monotonic(), out)
        return out

    def _install_offer(self, names: list[str], tools: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        items = [n for n in names if tools[n]["installable"]]
        if not items:
            return None
        size = sum(tools[n]["download_bytes"] for n in items)
        return {"items": items, "count": len(items), "download_bytes": size,
                "label": f"Install what's missing ({len(items)} item{'s' if len(items) != 1 else ''}, {_mb(size)})"}

    def _overall(self, out: dict[str, Any], tools, missing_required, missing_core, services, host, queue) -> dict[str, Any]:
        titles = lambda ns: _join([tools[n]["title"] for n in ns])  # noqa: E731
        broken = [n for n, t in tools.items() if t["state"] in ("corrupt", "broken") and (t["needed_by"] or t["required"])]
        if queue.get("running"):
            cur = queue.get("current")
            pct = queue.get("percent")
            return {"overall": "busy", "sentence": f"Installing {queue['done'] + 1} of {queue['total']}: "
                    f"{tools[cur]['title'] if cur in tools else cur}{f' ({int(pct)}% of all downloads)' if pct is not None else ''}.",
                    "fix": {"kind": "open_tools", "label": "Show progress"}}
        host_err = [h for h in host if h["state"] == "error"]
        if host_err:
            h = host_err[0]
            return {"overall": "error", "sentence": h["sentence"], "fix": {"kind": "recheck", "label": "Recheck"}, "next_action": h.get("next_action")}
        if broken:
            n = broken[0]
            why = (tools[n]["smoke"] or {}).get("message") if tools[n]["state"] == "broken" else "its files are missing or changed"
            return {"overall": "error", "sentence": f"{tools[n]['title']} is damaged ({why}).",
                    "fix": {"kind": "repair", "items": [n], "confirm": True, "label": f"Repair {tools[n]['title']}"}}
        unfixable = [n for n in missing_required if not tools[n]["installable"] and tools[n]["state"] != "installing"]
        if missing_required:
            projects = sorted({b["name"] for n in missing_required for b in tools[n]["needed_by"] if b["kind"] == "project" and b["required"]})
            offer = self._install_offer(missing_required, tools)
            s = (f"{len(missing_required)} tool{'s' if len(missing_required) != 1 else ''} your projects need "
                 f"{'are' if len(missing_required) != 1 else 'is'} missing: {titles(missing_required)}"
                 f"{f' (needed by {_join(projects)})' if projects else ''}.")
            if unfixable:
                s += f" {titles(unfixable)} cannot be installed: {tools[unfixable[0]].get('blocked_reason') or 'no verified download is pinned'}"
            return {"overall": "error", "sentence": s, "fix": ({"kind": "install", **offer} if offer else {"kind": "open_tools", "label": "Open Tools"})}
        down = [s for s in services if s["required"] and s["state"] in ("down", "error", "missing")]
        if down:
            s = down[0]
            return {"overall": "error", "sentence": s["sentence"], "fix": s.get("action") or {"kind": "recheck", "label": "Recheck"},
                    "next_action": s.get("next_action")}
        if queue.get("state") == "interrupted":
            return {"overall": "warn", "sentence": "An install was interrupted when Rebuild Studio closed.",
                    "fix": {"kind": "resume_queue", "label": "Resume install"}}
        if queue.get("state") == "failed" and queue.get("failed"):
            return {"overall": "warn", "sentence": f"{titles([n for n in queue['failed'] if n in tools])} could not be installed.",
                    "fix": {"kind": "retry_queue", "label": "Retry"}}
        if missing_core:
            offer = self._install_offer(missing_core, tools)
            return {"overall": "warn", "sentence": f"Recommended analysis tools are not installed yet: {titles(missing_core)}.",
                    "fix": ({"kind": "install", **offer} if offer else {"kind": "open_tools", "label": "Open Tools"})}
        soft = [s for s in services if s["state"] in ("down", "error")] + [h for h in host if h["state"] == "warn"]
        if soft:
            s = soft[0]
            return {"overall": "warn", "sentence": s["sentence"], "fix": s.get("action") or {"kind": "recheck", "label": "Recheck"},
                    "next_action": s.get("next_action")}
        return {"overall": "ok", "sentence": "Everything your projects need is installed and working.", "fix": None}

    def summary(self) -> dict[str, Any]:
        """Compact form for the doctor report."""
        r = self.report()
        return {"overall": r["overall"], "sentence": r["sentence"], "missing_required": r["missing_required"],
                "missing_recommended": r["missing_recommended"],
                "tools": {t["name"]: t["state"] for t in r["tools"]},
                "services": {s["name"]: s["state"] for s in r["services"]}, "host": {h["name"]: h["state"] for h in r["host"]}}

    # -- preflight before Start ----------------------------------------------------------------------------------------
    def preflight(self, case_id: str) -> dict[str, Any]:
        case = self.st.cases.get_case(case_id)
        cn = self.case_needs(case)
        names = list(self.setup._tools())
        tools = {n: self.tool_state(n) for n in names}
        missing = self._closure(list(cn["required"]), tools)
        optional = [n for n in self._closure(list(cn["optional"]), tools) if tools[n]["installable"] and n not in missing]

        def row(n: str) -> dict[str, Any]:
            t = tools[n]
            why = cn["required"].get(n) or cn["optional"].get(n) or [f"needed by {_join([tools[d]['title'] for d in tools if n in tools[d]['requires']] or ['another tool'])}"]
            return {"name": n, "title": t["title"], "state": t["state"], "why": why[0], "download_bytes": t["download_bytes"],
                    "installable": t["installable"], "blocked_reason": t["blocked_reason"], "job": t["job"]}
        services = []
        for s, sn in cn["services"].items():
            r = self._service(s, sn, tools)
            r.setdefault("title", ((self.needs().get("services") or {}).get(s) or {}).get("title") or s)
            r["required"] = sn["required"]
            r["why"] = sn["why"]
            services.append(r)
        blocking_services = [s for s in services if s["required"] and s["state"] in ("down", "error")]
        need_bytes = sum(tools[n]["disk_bytes"] + tools[n]["download_bytes"] for n in missing)
        host = [h for h in (self._chk_write(), self._chk_disk(need_bytes)) if h["state"] == "error"]
        offer = self._install_offer(missing, tools)
        installing = self.queue.busy() and any(it["name"] in missing and it["status"] in ("queued", "installing")
                                               for it in self.queue.snapshot()["items"])
        unfixable = [n for n in missing if not tools[n]["installable"] and tools[n]["state"] != "installing"]
        ok = not missing and not blocking_services
        if ok:
            sentence = "Everything this project needs is installed and running."
        elif installing:
            sentence = f"Installing what this project needs: {_join([tools[n]['title'] for n in missing])}. Start is available when it finishes."
        elif missing:
            sentence = (f"This project needs {_join([tools[n]['title'] for n in missing])} before it can start "
                        f"({cn['profile_title']} → {cn['target']}).")
            if unfixable:
                sentence += f" {_join([tools[n]['title'] for n in unfixable])} cannot be installed: {tools[unfixable[0]].get('blocked_reason')}"
        else:
            sentence = blocking_services[0]["sentence"]
        return {"case_id": case_id, "ok": ok, "installing": installing, "sentence": sentence,
                "profile": cn["profile"], "profile_title": cn["profile_title"], "target": cn["target"], "comparator": cn["comparator"],
                "ai": cn["ai"], "missing": [row(n) for n in missing], "optional": [row(n) for n in optional],
                "services": services, "blocking_services": [s["name"] for s in blocking_services], "host": host,
                "install": offer, "optional_install": self._install_offer(optional, tools), "settings": self.get_settings()}

    def require_ready(self, case_id: str) -> dict[str, Any]:
        pf = self.preflight(case_id)
        if not pf["ok"]:
            names = [m["title"] for m in pf["missing"]] or [self._service_title(s) for s in pf["blocking_services"]]
            raise ToolSetupError("dependencies_missing", pf["sentence"], affected=", ".join(names), status=409,
                                 next_action=(f"Press '{pf['install']['label']}' and start again." if pf["install"] else
                                              (next((s.get("next_action") or (s.get("action") or {}).get("label") for s in pf["services"]
                                                     if s["name"] in pf["blocking_services"]), None) or "Open Tools to see what is missing.")))
        return pf

    def _service_title(self, name: str) -> str:
        return ((self.needs().get("services") or {}).get(name) or {}).get("title") or name

    # -- installs ------------------------------------------------------------------------------------------------------
    def install(self, *, items: list[str] | None = None, case_id: str | None = None, repair: bool = False,
                include_optional: bool = False, reason: str = "user") -> dict[str, Any]:
        """One click: install the given items, or what a project needs, or everything every project (+ core) needs."""
        if items:
            names = list(items)
        elif case_id:
            pf = self.preflight(case_id)
            names = list((pf["install"] or {}).get("items") or [])
            if include_optional:
                names += list((pf["optional_install"] or {}).get("items") or [])
        else:
            r = self.report(refresh=True)
            names = list((r["install_all"] or {}).get("items") or [])
        if not names:
            raise ToolSetupError("nothing_to_install", "Nothing is missing.", status=409, next_action="Nothing to do.")
        snap = self.queue.start(names, repair=names if repair else (), reason=reason)
        self._report = None
        return snap

    def auto_install(self, reason: str) -> dict[str, Any] | None:
        """Only when the user turned on automatic installs: queue what projects need (+ core), never anything else."""
        if not self.get_settings()["auto_install"] or self.queue.busy() or self.setup._busy():
            return None
        try:
            return self.install(reason=reason)
        except ToolSetupError:
            return None

    def on_case_created(self, case_id: str) -> dict[str, Any] | None:
        try:
            pf = self.preflight(case_id)
        except Exception:  # noqa: BLE001
            return None
        if pf["install"] and self.get_settings()["auto_install"]:
            try:
                self.install(case_id=case_id, reason=f"project {case_id} created")
                pf = self.preflight(case_id)
            except ToolSetupError:
                pass
        return pf

    # -- startup: clean leftovers, report an interrupted install, resume it when allowed --------------------------------
    def startup(self) -> dict[str, Any]:
        cleaned = self.clean_leftovers()
        resumed = None
        if self.queue.snapshot().get("state") == "interrupted" and self.get_settings()["auto_install"]:
            try:
                resumed = self.queue.resume()
            except ToolSetupError:
                resumed = None
        if resumed is None:
            self.auto_install("startup")
        rep = self.report(refresh=True)
        return {"cleaned": cleaned, "resumed": bool(resumed), "overall": rep["overall"]}

    def startup_async(self) -> threading.Thread:
        def run() -> None:
            try:
                self.startup()
            except Exception:  # noqa: BLE001
                log.exception("startup dependency check failed")
        th = threading.Thread(target=run, name="dependency-startup", daemon=True)
        th.start()
        return th

    def clean_leftovers(self) -> list[str]:
        """Stale staging folders and partial downloads that can no longer be resumed (only while nothing installs)."""
        if self.setup._busy() or self.queue.busy():
            return []
        removed: list[str] = []
        tools = Path(self.setup.tools_dir)
        stage = tools / ".staging"
        if stage.is_dir():
            for p in stage.iterdir():
                try:
                    _rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
                    removed.append(str(p))
                except OSError as e:
                    log.info("could not remove stale %s: %s", p, e)
        dl = tools / ".downloads"
        if dl.is_dir():
            arts = {f"{(t.get('artifact') or {}).get('name')}.part": t.get("artifact") or {} for t in self.setup._tools().values()}
            for p in dl.glob("*.part"):           # only ToolSetup's own partial downloads; other files are left alone
                if not p.is_file():
                    continue
                art = arts.get(p.name)
                if art is None or not self.setup._resumable_bytes(p, art):
                    for q in (p, p.with_name(p.name + ".json")):
                        if q.exists():
                            q.unlink(missing_ok=True)
                            removed.append(str(q))
            for p in dl.glob("*.part.json"):
                if p.is_file() and not p.with_name(p.name[:-5]).exists():
                    p.unlink(missing_ok=True)
                    removed.append(str(p))
        return removed
