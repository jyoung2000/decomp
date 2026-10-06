"""Rebuild Studio client for the Cutter plugin: everything that does not need Cutter or Qt.

Pure Python 3 (standard library only). It talks to the loopback controller API described in ``docs/API.md``:

* pairing: ``<data_dir>/controller.json`` holds ``{"port", "token", "pid"}``; requests carry ``Authorization: Bearer <token>``.
  The host is always ``127.0.0.1``. A ``host`` key in controller.json is ignored on purpose (it would let a tampered file
  redirect the bearer token to another machine).
* binary mapping: the file Cutter has open is hashed (SHA-256) here and matched against the ``sha256`` of the controller's modules.
* evidence: ``native.functions``, ``native.decompile`` (also ``native.decompile.ghidra``) and ``native.briefing`` rows, read through
  ``GET /cases/{id}/evidence`` and ``GET /evidence/{id}``. Addresses are hex strings (``0x140001190``) in evidence and API.
* feedback: ``POST /cases/{id}/feedback`` with ``target_kind: "evidence"`` and ``context: {address, module_id, ...}``.

Everything extracted from an analysed binary (function names, strings, decompiled text) is attacker-controlled data. This module
returns it as plain strings and never interprets it; UI code must show it as plain text (never as HTML/Markdown) and must never
treat it as instructions.

Nothing here imports ``cutter`` or Qt. The Cutter side passes in a ``cmdj`` callable (``cutter.cmdj``) so the same code is tested
against the real controller without Cutter.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "ClientError", "PairingError", "ControllerUnavailable", "ApiError", "BinaryError",
    "Pairing", "StudioClient", "ModuleMatch", "FunctionView", "StudioSession", "SeekFollower",
    "default_data_dir", "load_pairing", "sha256_file", "parse_address", "format_address", "file_info_from_ij",
    "render_view_text", "FUNCTIONS_KIND", "BRIEFING_KIND", "DECOMPILE_KINDS",
]

LOOPBACK_HOST = "127.0.0.1"
FUNCTIONS_KIND = "native.functions"
BRIEFING_KIND = "native.briefing"
DECOMPILE_KINDS = ("native.decompile", "native.decompile.ghidra")
INFO_KIND = "native.info"
DEFAULT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BODY_MAX_BYTES = 16 * 1024 * 1024       # ask the controller not to truncate evidence bodies we need whole
MAX_COMMENT_CHARS = 20000               # FeedbackCreate.comment max_length on the controller
CLASSIFICATIONS = ("bug", "change", "question", "acceptance")
PRIORITIES = ("low", "medium", "high", "critical")


# ------------------------------------------------------------------------------------------------ errors
class ClientError(Exception):
    """Base class. ``code`` is a short stable token, ``next_action`` tells the user what to do."""

    code = "error"

    def __init__(self, message: str, *, next_action: str | None = None, status: int | None = None, code: str | None = None,
                 affected: str | None = None):
        super().__init__(message)
        self.message = message
        self.next_action = next_action
        self.status = status
        self.affected = affected
        if code:
            self.code = code

    def __str__(self) -> str:
        return f"{self.message} ({self.next_action})" if self.next_action else self.message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "next_action": self.next_action, "status": self.status}


class PairingError(ClientError):
    code = "not_paired"


class ControllerUnavailable(ClientError):
    code = "controller_unavailable"


class ApiError(ClientError):
    code = "api_error"


class BinaryError(ClientError):
    code = "binary"


# ------------------------------------------------------------------------------------------------ pairing
def default_data_dir() -> Path:
    """Same rule as the controller (``rebuild_controller.config.default_data_dir``), without importing it."""
    env = os.environ.get("REBUILD_STUDIO_DATA")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "RebuildStudio"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "rebuild-studio"


@dataclass(frozen=True)
class Pairing:
    port: int
    token: str = field(repr=False)          # never shown in repr/logs
    pid: int | None = None
    source: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}"


def load_pairing(data_dir: str | os.PathLike[str] | None = None, *, path: str | os.PathLike[str] | None = None) -> Pairing:
    """Read ``controller.json``. Lookup order: ``path``, ``$REBUILD_STUDIO_CONTROLLER_JSON``, ``data_dir``, the default data dir."""
    if path is None and data_dir is None:
        env = os.environ.get("REBUILD_STUDIO_CONTROLLER_JSON")
        if env:
            path = env
    p = Path(path) if path is not None else Path(data_dir) / "controller.json" if data_dir is not None else default_data_dir() / "controller.json"
    try:
        raw = p.read_text("utf-8")
    except OSError as e:
        raise PairingError(f"cannot read {p}: {e.strerror or e}",
                           next_action="start Rebuild Studio (it writes controller.json), or set REBUILD_STUDIO_DATA to its data folder") from e
    try:
        info = json.loads(raw)
        port = int(info["port"])
        token = info["token"]
        pid = int(info["pid"]) if info.get("pid") is not None else None
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise PairingError(f"{p} is not a valid controller.json ({type(e).__name__})",
                           next_action="restart Rebuild Studio so it rewrites controller.json") from e
    if not isinstance(token, str) or not token or not (0 < port < 65536):
        raise PairingError(f"{p} has an empty token or an invalid port", next_action="restart Rebuild Studio so it rewrites controller.json")
    return Pairing(port=port, token=token, pid=pid, source=str(p))


# ------------------------------------------------------------------------------------------------ HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # never forward the bearer token anywhere else
        return None


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) = ignore HTTP(S)_PROXY: loopback traffic must never go through a proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class StudioClient:
    """Thin typed wrapper over the controller REST API. All failures raise a :class:`ClientError` subclass."""

    def __init__(self, pairing: Pairing, *, timeout: float = DEFAULT_TIMEOUT):
        self.pairing = pairing
        self.timeout = timeout
        self._opener = _opener()

    @classmethod
    def from_data_dir(cls, data_dir: str | os.PathLike[str] | None = None, *, path: str | os.PathLike[str] | None = None,
                      timeout: float = DEFAULT_TIMEOUT) -> "StudioClient":
        return cls(load_pairing(data_dir, path=path), timeout=timeout)

    # -- transport
    def _request(self, method: str, path: str, *, query: dict[str, Any] | None = None, body: Any = None, auth: bool = True) -> Any:
        url = self.pairing.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.pairing.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read(MAX_RESPONSE_BYTES + 1)
                status = resp.status
        except urllib.error.HTTPError as e:
            raise self._api_error(e) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ControllerUnavailable(f"cannot reach the Rebuild Studio controller at {self.pairing.base_url}: {reason}",
                                        next_action="start Rebuild Studio; if it was restarted, reconnect to re-read controller.json") from e
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ApiError("controller response exceeds the size limit", status=status, code="too_large",
                           next_action="narrow the request")
        try:
            return json.loads(payload.decode("utf-8")) if payload else None
        except ValueError as e:
            raise ApiError("controller returned a non-JSON response", status=status, code="bad_response",
                           next_action="check that controller.json points at the Rebuild Studio controller") from e

    @staticmethod
    def _api_error(e: urllib.error.HTTPError) -> ClientError:
        err: dict[str, Any] = {}
        try:
            parsed = json.loads(e.read(1_000_000).decode("utf-8", "replace"))
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                err = parsed["error"]
        except (ValueError, OSError):
            pass
        code = str(err.get("code") or f"http_{e.code}")
        msg = str(err.get("message") or e.reason or f"HTTP {e.code}")
        if e.code == 401:
            return ApiError("the controller rejected the token (it changes on every launch)", status=401, code="auth",
                            next_action="reconnect to re-read controller.json after restarting Rebuild Studio")
        return ApiError(msg, status=e.code, code=code, affected=err.get("affected"), next_action=err.get("next_action"))

    # -- endpoints used by the plugin
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", auth=False)

    def list_cases(self) -> list[dict[str, Any]]:
        return self._request("GET", "/cases") or []

    def list_modules(self, case_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/cases/{_seg(case_id)}/modules") or []

    def list_evidence(self, case_id: str, *, kind: str | None = None, module_id: str | None = None) -> list[dict[str, Any]]:
        return self._request("GET", f"/cases/{_seg(case_id)}/evidence", query={"kind": kind, "module_id": module_id}) or []

    def get_evidence(self, evidence_id: str, *, max_bytes: int = BODY_MAX_BYTES) -> dict[str, Any]:
        return self._request("GET", f"/evidence/{_seg(evidence_id)}", query={"max_bytes": max_bytes})

    def create_feedback(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/cases/{_seg(case_id)}/feedback", body=payload)


def _seg(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


# ------------------------------------------------------------------------------------------------ helpers
def sha256_file(path: str | os.PathLike[str], *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError as e:
        raise BinaryError(f"cannot read {path}: {e.strerror or e}", next_action="open a file that exists on this machine in Cutter") from e
    return h.hexdigest()


def parse_address(value: Any) -> int:
    """Accept ints and hex strings. Strings are always hex, with or without the ``0x`` prefix (the API's address convention)."""
    if isinstance(value, bool):
        raise ValueError("not an address")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("negative address")
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            raise ValueError("empty address")
        return int(s[2:] if s.startswith("0x") else s, 16)
    raise ValueError(f"not an address: {type(value).__name__}")


def format_address(addr: int) -> str:
    return f"0x{int(addr):x}"


def file_info_from_ij(ij: Any) -> dict[str, Any]:
    """Pull the open file out of rizin's ``ij`` JSON. ``core.file`` is a string in rizin 0.9; ``core.file.path`` is tolerated too."""
    if not isinstance(ij, dict):
        raise BinaryError("Cutter returned no file information (ij)", next_action="open a binary in Cutter first")
    core = ij.get("core") if isinstance(ij.get("core"), dict) else {}
    f = core.get("file")
    if isinstance(f, dict):
        f = f.get("path")
    if not isinstance(f, str) or not f:
        raise BinaryError("Cutter has no file open", next_action="open a binary in Cutter first")
    if f.startswith("file://"):
        f = urllib.parse.unquote(urllib.parse.urlparse(f).path)
        if os.name == "nt" and len(f) > 2 and f[0] == "/" and f[2] == ":":
            f = f[1:]
    if "://" in f:
        raise BinaryError(f"the open target {f.split('://')[0]}:// is not a plain file, so it cannot be matched by hash",
                          next_action="open the binary from disk (File > Open) instead of a debugger/memory target")
    b = ij.get("bin") if isinstance(ij.get("bin"), dict) else {}
    baddr = b.get("baddr")
    return {"path": f, "baddr": baddr if isinstance(baddr, int) and not isinstance(baddr, bool) else None,
            "arch": b.get("arch"), "bits": b.get("bits"), "format": core.get("format")}


def _addr_of(row: dict[str, Any]) -> int | None:
    a = (row.get("meta") or {}).get("address")
    try:
        return parse_address(a) if a is not None else None
    except ValueError:
        return None


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(rows, key=lambda r: (r.get("revision") or 0, r.get("created_at") or "")) if rows else None


# ------------------------------------------------------------------------------------------------ session model
@dataclass
class ModuleMatch:
    case_id: str
    case_name: str
    module_id: str
    rel_path: str
    sha256: str
    created_at: str = ""
    has_functions: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class FunctionView:
    """What the dock shows for the address under the cursor. ``status`` drives the UI; ``message`` is always human readable."""
    status: str                         # ready | no_function | not_mapped | unavailable | no_evidence | error
    message: str = ""
    address: str | None = None          # the cursor address
    case_id: str | None = None
    module_id: str | None = None
    function: dict[str, Any] | None = None       # {name, addr, size, ...}
    source: str | None = None                    # briefing | decompile | functions
    briefing: dict[str, Any] | None = None
    decompiled_text: str | None = None
    decompiler: str | None = None
    is_real_decompiler: bool | None = None
    evidence_ids: list[str] = field(default_factory=list)
    target_evidence_id: str | None = None        # what feedback attaches to
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    next_action: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def key(self) -> tuple[Any, ...]:
        return (self.status, self.module_id, (self.function or {}).get("addr"), self.source, tuple(self.evidence_ids))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class StudioSession:
    """Binds one open Cutter binary to a controller module and serves per-address views.

    ``cmdj`` is ``cutter.cmdj`` inside Cutter (a callable ``str -> parsed JSON``). ``client_factory`` lets callers re-pair
    (controller.json changes on every launch); by default it re-reads controller.json from ``data_dir``.
    """

    def __init__(self, cmdj: Callable[[str], Any], *, data_dir: str | os.PathLike[str] | None = None,
                 controller_json: str | os.PathLike[str] | None = None, client: StudioClient | None = None,
                 cache_ttl: float = 5.0, index_ttl: float = 30.0, timeout: float = DEFAULT_TIMEOUT,
                 clock: Callable[[], float] = time.monotonic):
        self._cmdj = cmdj
        self._data_dir = data_dir
        self._controller_json = controller_json
        self._timeout = timeout
        self._client = client
        self._ttl = cache_ttl
        self._index_ttl = index_ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self._pinned_case: str | None = None
        self._file_info: dict[str, Any] | None = None     # last result of observe_file()
        self._file_error: BinaryError | None = None
        self.file_path: str | None = None
        self.file_sha256: str | None = None
        self.cutter_baddr: int | None = None
        self.matches: list[ModuleMatch] = []
        self.binding: ModuleMatch | None = None
        # (fetched_at, (case_id, module_id), evidence_id, functions, base-address warning)
        self._functions: tuple[float, tuple[str, str], str, list[dict[str, Any]], str | None] | None = None
        self._view_cache: dict[tuple[str, str, int], tuple[float, FunctionView]] = {}
        self._body_cache: dict[str, Any] = {}

    # -- connection
    @property
    def client(self) -> StudioClient:
        with self._lock:
            if self._client is None:
                self._client = StudioClient.from_data_dir(self._data_dir, path=self._controller_json, timeout=self._timeout)
            return self._client

    def reconnect(self) -> dict[str, Any]:
        """Drop all state, re-read controller.json, and check ``/health``."""
        with self._lock:
            self._client = None
            self.invalidate()
            self.binding = None
            self.matches = []
        h = self.client.health()
        return {"ok": bool(h.get("ok")), "version": h.get("version"), "base_url": self.client.pairing.base_url,
                "source": self.client.pairing.source}

    def invalidate(self) -> None:
        with self._lock:
            self._functions = None
            self._view_cache.clear()
            self._body_cache.clear()

    def pin_case(self, case_id: str | None) -> None:
        """Choose one case when the same binary is registered in several. ``None`` goes back to the automatic choice."""
        with self._lock:
            self._pinned_case = case_id
            self.binding = next((m for m in self.matches if m.case_id == case_id), None) if case_id else None
            self.invalidate()

    # -- binary mapping
    def current_file(self) -> dict[str, Any]:
        try:
            ij = self._cmdj("ij")
        except Exception as e:  # cutter.cmdj raises ValueError on non-JSON output
            raise BinaryError(f"cannot ask Cutter for the open file: {type(e).__name__}: {e}", next_action="open a binary in Cutter first") from e
        return file_info_from_ij(ij)

    def observe_file(self) -> dict[str, Any]:
        """Ask Cutter which file is open. Call this on the thread that owns Cutter (the GUI thread), never from a worker:
        the result is stored and the worker-side code only uses the stored value."""
        try:
            info = self.current_file()
        except BinaryError as e:
            with self._lock:
                self._file_info, self._file_error = None, e
            raise
        with self._lock:
            self._file_info, self._file_error = info, None
        return info

    def _hash(self, path: str) -> str:
        try:
            st = os.stat(path)
        except OSError as e:
            raise BinaryError(f"cannot read {path}: {e.strerror or e}", next_action="open a file that exists on this machine in Cutter") from e
        key = (path, st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._hash_cache.get(key)
        if hit:
            return hit
        digest = sha256_file(path)
        with self._lock:
            self._hash_cache = {key: digest}      # keep one entry: the currently open file
        return digest

    def find_modules(self, sha256: str) -> list[ModuleMatch]:
        """All controller modules whose sha256 equals ``sha256``, best first (has native.functions, then newest case)."""
        sha = sha256.lower()
        out: list[ModuleMatch] = []
        for case in self.client.list_cases():
            cid = case.get("case_id")
            if not cid:
                continue
            for m in self.client.list_modules(cid):
                if str(m.get("sha256", "")).lower() == sha:
                    has = bool(self.client.list_evidence(cid, kind=FUNCTIONS_KIND, module_id=m["module_id"]))
                    out.append(ModuleMatch(case_id=cid, case_name=str(case.get("name") or ""), module_id=m["module_id"],
                                           rel_path=str(m.get("rel_path") or ""), sha256=sha,
                                           created_at=str(case.get("created_at") or ""), has_functions=has))
        out.sort(key=lambda x: (x.has_functions, x.created_at), reverse=True)
        return out

    def attach(self, info: dict[str, Any] | None = None) -> ModuleMatch:
        """(Re)bind to the file Cutter has open (``info`` from :meth:`observe_file`, or asked now).

        Raises :class:`BinaryError` when no controller module has this file's hash."""
        info = info or self._file_info or self.observe_file()
        path = info["path"]
        sha = self._hash(path)
        with self._lock:
            changed = (sha != self.file_sha256)
            self.file_path, self.file_sha256, self.cutter_baddr = path, sha, info.get("baddr")
            if changed:
                self.invalidate()
                self.binding = None
        matches = self.find_modules(sha)
        with self._lock:
            self.matches = matches
            if not matches:
                self.binding = None
                raise BinaryError(f"no Rebuild Studio case contains this file (sha256 {sha[:12]}...)",
                                  next_action="create a case over the folder that holds this exact file, or open the file the case analysed "
                                              "(a patched or re-saved copy has a different hash)")
            pick = next((m for m in matches if m.case_id == self._pinned_case), None) or matches[0]
            self.binding = pick
            return pick

    # -- evidence access
    def _body(self, evidence_id: str) -> Any:
        with self._lock:
            if evidence_id in self._body_cache:
                return self._body_cache[evidence_id]
        ev = self.client.get_evidence(evidence_id)
        body = ev.get("body")
        with self._lock:
            if len(self._body_cache) > 64:
                self._body_cache.pop(next(iter(self._body_cache)))
            self._body_cache[evidence_id] = body
        return body

    def function_index(self, *, force: bool = False) -> tuple[str, list[dict[str, Any]], str | None]:
        """``(evidence_id, functions, baddr_warning)`` from the newest non-stale ``native.functions`` evidence."""
        b = self.binding
        if b is None:
            raise BinaryError("no module is bound yet", next_action="attach to the open binary first")
        key = (b.case_id, b.module_id)
        with self._lock:
            f = self._functions
            if f and not force and f[1] == key and self._clock() - f[0] < self._index_ttl:
                return f[2], f[3], f[4]
        rows = self.client.list_evidence(b.case_id, kind=FUNCTIONS_KIND, module_id=b.module_id)
        row = _latest(rows)
        if row is None:
            raise ApiError("the controller has no native.functions evidence for this module yet", code="no_evidence",
                           next_action="start the case in Rebuild Studio and wait for the analyze_module job to finish")
        body = self._body(row["evidence_id"])
        funcs = body.get("functions") if isinstance(body, dict) else None
        if not isinstance(funcs, list):
            raise ApiError("native.functions evidence is truncated or malformed", code="bad_evidence",
                           next_action="re-run analysis for this module")
        warn = self._baddr_warning(b)
        with self._lock:
            self._functions = (self._clock(), key, row["evidence_id"], funcs, warn)
        return row["evidence_id"], funcs, warn

    def _baddr_warning(self, b: ModuleMatch) -> str | None:
        """Best-effort image-base check: Cutter's ``bin.baddr`` (ij) against the controller's ``native.info``."""
        if self.cutter_baddr is None:
            return None
        try:
            row = _latest(self.client.list_evidence(b.case_id, kind=INFO_KIND, module_id=b.module_id))
            body = self._body(row["evidence_id"]) if row else None
            theirs = (body or {}).get("info", {}).get("baddr") if isinstance(body, dict) else None
        except ClientError:
            return None
        if isinstance(theirs, int) and theirs != self.cutter_baddr:
            return (f"Cutter loaded this file at base {format_address(self.cutter_baddr)} but the controller analysed it at "
                    f"{format_address(theirs)}; addresses will not line up until both use the same base")
        return None

    @staticmethod
    def containing_function(funcs: list[dict[str, Any]], addr: int) -> dict[str, Any] | None:
        """The function whose [minbound, maxbound) range holds ``addr``; for nested ranges the innermost (highest start) wins."""
        best: tuple[int, dict[str, Any]] | None = None
        for f in funcs:
            if not isinstance(f, dict):
                continue
            off = f.get("offset")
            if not isinstance(off, int) or isinstance(off, bool):
                try:
                    off = parse_address(f.get("addr"))
                except ValueError:
                    continue
            lo = f["minbound"] if isinstance(f.get("minbound"), int) else off
            hi = f["maxbound"] if isinstance(f.get("maxbound"), int) else off + int(f.get("size") or 0)
            lo = min(lo, off)
            if lo <= addr < max(hi, off + 1) and (best is None or lo >= best[0]):
                best = (lo, {**f, "offset": off})
        return best[1] if best else None

    # -- the main entry: view for the cursor
    def view_for_address(self, address: Any, *, force: bool = False) -> FunctionView:
        """Never raises: every failure becomes a ``FunctionView`` with ``status`` and ``next_action``."""
        try:
            addr = parse_address(address)
        except ValueError as e:
            return FunctionView(status="error", message=f"bad address: {e}")
        a = format_address(addr)
        try:
            with self._lock:
                info, err = self._file_info, self._file_error
            if err is not None:
                raise err
            if info is None:
                info = self.observe_file()      # only reached when nobody observed Cutter yet (sync callers, tests)
            if self.binding is None or force or info["path"] != self.file_path:
                self.attach(info)
            b = self.binding
            assert b is not None
            if force:
                self.invalidate()
            fn_ev_id, funcs, warn = self.function_index(force=force)
            fn = self.containing_function(funcs, addr)
            if fn is None:
                return FunctionView(status="no_function", address=a, case_id=b.case_id, module_id=b.module_id,
                                    message=f"No analysed function contains {a}.", warnings=[warn] if warn else [])
            key = (b.case_id, b.module_id, fn["offset"])
            with self._lock:
                hit = self._view_cache.get(key)
            if hit and self._clock() - hit[0] < self._ttl:
                v = hit[1]
                return FunctionView(**{**v.__dict__, "address": a})
            view = self._build_view(b, fn, a, warn, fn_ev_id)
            with self._lock:
                self._view_cache[key] = (self._clock(), view)
            return view
        except BinaryError as e:
            return FunctionView(status="not_mapped", address=a, message=e.message, next_action=e.next_action,
                                case_id=self.binding.case_id if self.binding else None)
        except ControllerUnavailable as e:
            return FunctionView(status="unavailable", address=a, message=e.message, next_action=e.next_action)
        except PairingError as e:
            return FunctionView(status="unavailable", address=a, message=e.message, next_action=e.next_action)
        except ApiError as e:
            st = "no_evidence" if e.code == "no_evidence" else "error"
            return FunctionView(status=st, address=a, message=e.message, next_action=e.next_action,
                                case_id=self.binding.case_id if self.binding else None)
        except Exception as e:  # keep the dock alive whatever happens
            return FunctionView(status="error", address=a, message=f"{type(e).__name__}: {e}")

    def _build_view(self, b: ModuleMatch, fn: dict[str, Any], cursor: str, warn: str | None, fn_ev_id: str | None) -> FunctionView:
        faddr = int(fn["offset"])
        finfo = {k: fn.get(k) for k in ("name", "addr", "size", "realsz", "nbbs", "cc", "signature", "calltype", "noreturn", "n_callrefs", "n_datarefs")}
        finfo["addr"] = finfo.get("addr") or format_address(faddr)
        warnings = [warn] if warn else []
        view = FunctionView(status="ready", address=cursor, case_id=b.case_id, module_id=b.module_id, function=finfo,
                            source="functions", target_evidence_id=fn_ev_id, evidence_ids=[fn_ev_id] if fn_ev_id else [], warnings=warnings)

        brief_rows = [r for r in self.client.list_evidence(b.case_id, kind=BRIEFING_KIND, module_id=b.module_id) if _addr_of(r) == faddr]
        brow = _latest(brief_rows)
        if brow is not None:
            body = self._body(brow["evidence_id"])
            if isinstance(body, dict) and isinstance(body.get("function"), dict):
                view.source, view.briefing = "briefing", body
                view.target_evidence_id = brow["evidence_id"]
                view.evidence_ids = [brow["evidence_id"], *[e for e in body.get("evidence_ids", []) if isinstance(e, str) and e != brow["evidence_id"]]]
                dec = body.get("decompiled") or {}
                view.decompiled_text = dec.get("text")
                view.decompiler, view.is_real_decompiler = dec.get("decompiler"), dec.get("is_real_decompiler")
                view.truncated = bool(body.get("any_truncated"))
                return view
            warnings.append("a briefing evidence row exists but its body could not be read; showing other evidence")

        drows: list[dict[str, Any]] = []
        for kind in DECOMPILE_KINDS:
            drows += [r for r in self.client.list_evidence(b.case_id, kind=kind, module_id=b.module_id) if _addr_of(r) == faddr]
        drow = _latest(drows)
        if drow is not None:
            body = self._body(drow["evidence_id"])
            if isinstance(body, dict):
                dec = body.get("decompiled") if isinstance(body.get("decompiled"), dict) else {}
                text = dec.get("text") if dec else body.get("text")
                view.source, view.decompiled_text = "decompile", text if isinstance(text, str) else None
                view.decompiler = body.get("decompiler") or (drow.get("meta") or {}).get("decompiler")
                view.is_real_decompiler = body.get("is_real_decompiler")
                view.truncated = bool(dec.get("truncated")) or bool((drow.get("meta") or {}).get("truncated"))
                view.target_evidence_id = drow["evidence_id"]
                view.evidence_ids = [drow["evidence_id"], *view.evidence_ids]
                view.message = "No briefing evidence for this function yet; showing the decompiled text."
                return view
        view.message = ("No briefing or decompile evidence for this function yet; showing function metadata only. "
                        "Ask your AI client for get_function_briefing on this address, or raise the case's decompile_limit.")
        view.next_action = "request a function briefing from Rebuild Studio (MCP get_function_briefing)"
        return view

    # -- feedback
    def submit_feedback(self, view: FunctionView, comment: str, *, classification: str = "question", priority: str = "medium",
                        expected: str = "", actual: str = "") -> dict[str, Any]:
        """Persist an annotation as controller feedback on the evidence behind ``view``."""
        if not view.ok or not view.case_id or not view.module_id or not view.target_evidence_id or not view.function:
            raise ApiError("there is no analysed function under the cursor to attach this note to", code="no_target",
                           next_action="move the cursor into an analysed function first")
        comment = (comment or "").strip()
        if not comment:
            raise ApiError("the note is empty", code="empty", next_action="type a note first")
        if len(comment) > MAX_COMMENT_CHARS:
            raise ApiError(f"the note is longer than {MAX_COMMENT_CHARS} characters", code="too_long", next_action="shorten it")
        if classification not in CLASSIFICATIONS or priority not in PRIORITIES:
            raise ApiError("unknown classification or priority", code="rejected",
                           next_action=f"classification: {', '.join(CLASSIFICATIONS)}; priority: {', '.join(PRIORITIES)}")
        payload = {
            "target_kind": "evidence", "target_id": view.target_evidence_id, "classification": classification, "priority": priority,
            "comment": comment, "expected": expected, "actual": actual,
            "context": {"address": view.address or view.function.get("addr"), "function_address": view.function.get("addr"),
                        "function": view.function.get("name"), "module_id": view.module_id, "source": "cutter",
                        "evidence_ids": view.evidence_ids, "file_sha256": self.file_sha256},
        }
        return self.client.create_feedback(view.case_id, payload)


# ------------------------------------------------------------------------------------------------ following the cursor
class SeekFollower:
    """Turns ``seekChanged`` events into views without ever blocking the caller (when ``run_async``).

    Rapid seeks coalesce: only the newest address is fetched, and a result that has been superseded is dropped.
    ``on_view`` is called from the worker thread; the Qt layer re-emits it through a Qt signal to reach the GUI thread.
    Consecutive seeks inside the same function do not re-emit unless ``force``.
    """

    def __init__(self, session: StudioSession, on_view: Callable[[FunctionView], None], *, run_async: bool = True):
        self._session = session
        self._on_view = on_view
        self.run_async = run_async
        self._lock = threading.Lock()
        self._pending: tuple[int, bool] | None = None
        self._running = False
        self._idle = threading.Event()
        self._idle.set()
        self._last_key: tuple[Any, ...] | None = None
        self.enabled = True

    def on_seek(self, address: Any, *, force: bool = False) -> None:
        if not self.enabled and not force:
            return
        try:
            addr = parse_address(address)
        except ValueError:
            return
        try:
            self._session.observe_file()   # talks to Cutter, so it runs on the caller's (GUI) thread; errors are kept for the view
        except Exception:
            pass
        with self._lock:
            self._pending = (addr, force)
            if self._running:
                return
            self._running = True
            self._idle.clear()
        if self.run_async:
            threading.Thread(target=self._drain, name="rebuild-studio-seek", daemon=True).start()
        else:
            self._drain()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        return self._idle.wait(timeout)

    def _drain(self) -> None:
        while True:
            with self._lock:
                item, self._pending = self._pending, None
                if item is None:
                    self._running = False
                    self._idle.set()
                    return
            addr, force = item
            try:
                view = self._session.view_for_address(addr, force=force)
            except Exception as e:  # view_for_address should not raise; be safe in a thread
                view = FunctionView(status="error", message=f"{type(e).__name__}: {e}", address=format_address(addr))
            with self._lock:
                superseded = self._pending is not None
                key = view.key()
                same = (key == self._last_key) and not force
                if not superseded:
                    self._last_key = key
            if superseded or same:
                continue
            try:
                self._on_view(view)
            except Exception:
                pass


# ------------------------------------------------------------------------------------------------ plain-text rendering
def _s(v: Any, limit: int = 400) -> str:
    t = "" if v is None else str(v)
    return t if len(t) <= limit else t[:limit] + "...[truncated]"


def render_view_text(view: FunctionView, *, max_items: int = 40) -> str:
    """Plain-text rendering of the briefing part (not the decompiled text). Safe for QPlainTextEdit / QLabel(PlainText) only."""
    lines: list[str] = []
    if view.function:
        f = view.function
        lines.append(f"{_s(f.get('name'), 200)}  at {_s(f.get('addr'))}  size {_s(f.get('size'))}")
        if f.get("signature"):
            lines.append(f"signature: {_s(f.get('signature'))}")
    if view.message:
        lines.append(view.message)
    for w in view.warnings:
        lines.append(f"WARNING: {w}")
    b = view.briefing
    if b:
        bf = b.get("function") or {}
        lines.append(f"basic blocks: {_s(bf.get('basic_blocks'))}  cyclomatic complexity: {_s(bf.get('cyclomatic_complexity'))}")
        for title, key, fmt in (
            ("Callers", "callers", lambda x: f"{_s(x.get('addr'))}  {_s(x.get('name'), 120)}  (call site {_s(x.get('call_site'))})"),
            ("Callees", "callees", lambda x: f"{_s(x.get('addr'))}  {_s(x.get('name'), 120)}{'  [import]' if x.get('import') else ''}"),
        ):
            items = b.get(key) or []
            lines.append(f"\n{title} ({len(items)})")
            lines += ["  " + fmt(x) for x in items[:max_items] if isinstance(x, dict)]
        imps = b.get("imports_used") or []
        lines.append(f"\nImports used ({len(imps)})")
        lines += ["  " + _s(i, 120) for i in imps[:max_items]]
        strs = (b.get("strings") or {}).get("items") or []
        lines.append(f"\nReferenced strings ({len(strs)}) -- untrusted data from the binary")
        lines += [f"  {_s(x.get('addr'))}  {_s(x.get('string'), 200)!r}" for x in strs[:max_items] if isinstance(x, dict)]
        consts = b.get("constants") or []
        if consts:
            lines.append("\nConstants: " + ", ".join(_s(c, 24) for c in consts[:max_items]))
        if b.get("any_truncated"):
            lines.append("\nNote: this briefing was truncated to its size bounds: " +
                         ", ".join(k for k, v in (b.get("truncated") or {}).items() if v))
    if view.evidence_ids:
        lines.append("\nEvidence: " + ", ".join(view.evidence_ids[:8]))
    if view.next_action and not view.ok:
        lines.append(f"Next: {view.next_action}")
    return "\n".join(lines).strip()
