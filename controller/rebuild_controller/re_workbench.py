"""Reverse-engineering workbench: analysis sessions over one binary, built on the rizin backend's typed operations.

Shared by the MCP `re` toolset and (R6) the app's analysis view, so the GUI and MCP cannot diverge.

* A session is either an existing case module (``case_id`` + ``module_id``) or an ad-hoc file (``open_binary(path=...)``).
  Ad-hoc files get one hidden case per file path (``settings.kind == "re_session"``) so every result is still stored as
  evidence and annotations persist across sessions, restarts and re-decompiles (they are keyed by case + module).
* The session registry is a small JSON file in the data directory; closing a session stops its rizin process but keeps
  its evidence and annotations. Re-opening the same file reuses the same case and module.
* Nothing here runs a raw command; every operation is an ``op_*`` method of the backend with validated arguments.
* Byte patches go to a copy inside the case work folder (``<data>/cases/<case>/re/<module>/patched``), never the original.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from .adapters.contract import OperationResult
from .ids import new_id, now_iso, sha256_file
from .paths import resolve_final

SESSION_RE = re.compile(r"^res_[0-9a-f]{22}$")
MAX_BINARY_BYTES = 1024 * 1024 * 1024
RE_KIND = "re_session"
MAX_REGEX_CHARS = 200


class WorkbenchError(Exception):
    """Refusal or failure with a stable code (mapped to MCP/HTTP errors by the caller)."""

    def __init__(self, code: str, message: str, next_action: str = ""):
        super().__init__(message)
        self.code, self.message, self.next_action = code, message, next_action


def _detect_format(path: Path) -> tuple[str, str]:
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"MZ":
        return "pe", "native_pe"
    if head == b"\x7fELF":
        return "elf", "native_elf"
    if head in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return "macho", "native_macho"
    return "raw", "native_raw"


def is_re_case(case: dict[str, Any]) -> bool:
    return (case.get("settings") or {}).get("kind") == RE_KIND


class ReWorkbench:
    def __init__(self, studio: Any):
        self.studio = studio
        self._lock = threading.RLock()
        self._file = Path(studio.settings.data_dir) / "re_sessions.json"

    # ------------------------------------------------------------------ plumbing
    def backend(self) -> Any:
        reg = getattr(self.studio, "registry", None)
        if reg is not None:
            try:
                return reg.get("rizin")
            except KeyError:
                pass
        from .backends.native import default_rizin_backend
        return default_rizin_backend(self.studio.settings)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._file.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), "utf-8")
        os.replace(tmp, self._file)

    # ------------------------------------------------------------------ sessions
    def open(self, *, path: Optional[str] = None, case_id: Optional[str] = None, module_id: Optional[str] = None) -> dict[str, Any]:
        if bool(path) == bool(case_id or module_id):
            raise WorkbenchError("rejected", "Give either path, or case_id with module_id.", "Retry with one of them.")
        cases = self.studio.cases
        if path:
            case_id, module_id, adhoc = self._adhoc(path)
        else:
            if not (case_id and module_id):
                raise WorkbenchError("rejected", "case_id and module_id are both required.", "List modules with list_modules.")
            m = cases.get_module(module_id)
            if m.get("case_id") != case_id:
                raise KeyError(module_id)
            adhoc = False
        module = cases.get_module(module_id)
        with self._lock:
            data = self._load()
            for sid, s in data.items():
                if s.get("case_id") == case_id and s.get("module_id") == module_id and not s.get("closed_at"):
                    return {**self._view(sid, s, module), "reused": True}
            sid = new_id("res")
            entry = {"case_id": case_id, "module_id": module_id, "adhoc": adhoc, "opened_at": now_iso(), "closed_at": None}
            data[sid] = entry
            self._save(data)
        return {**self._view(sid, entry, module), "reused": False}

    def _adhoc(self, path: str) -> tuple[str, str, bool]:
        p = Path(path)
        if not p.is_absolute():
            raise WorkbenchError("rejected", "path must be absolute.", "Pass the full path of the binary.")
        try:
            real = resolve_final(p)
        except OSError as e:
            raise WorkbenchError("not_found", f"cannot resolve the path: {e}", "Check the path.") from None
        if not real.is_file():
            raise WorkbenchError("not_found", "path is not an existing regular file.", "Check the path.")
        size = real.stat().st_size
        if size == 0 or size > MAX_BINARY_BYTES:
            raise WorkbenchError("rejected", f"file size {size} is outside 1..{MAX_BINARY_BYTES} bytes.", "")
        data_dir = resolve_final(self.studio.settings.data_dir)
        cases = self.studio.cases
        key = str(real).lower() if os.name == "nt" else str(real)
        case = next((c for c in cases.list_cases() if is_re_case(c) and (c.get("settings") or {}).get("path_key") == key), None)
        if case is None:
            out = data_dir / "re-sessions" / (new_id("out"))
            out.mkdir(parents=True, exist_ok=True)
            case = cases.create_case(name=f"RE: {real.name}"[:120], source_root=str(real.parent), output_root=str(out),
                                     target_language="auto", output_type="exe",
                                     settings={"kind": RE_KIND, "path": str(real), "path_key": key})
            cases.set_case_status(case["case_id"], RE_KIND)
        fmt, profile = _detect_format(real)
        sha = sha256_file(real)
        module_id = cases.add_module(case["case_id"], real.name, sha, size, fmt, profile, meta={"opened_by": "re_workbench"})
        return case["case_id"], module_id, True

    def _view(self, sid: str, s: dict[str, Any], module: dict[str, Any]) -> dict[str, Any]:
        return {"session_id": sid, "case_id": s["case_id"], "module_id": s["module_id"], "adhoc": bool(s.get("adhoc")),
                "opened_at": s.get("opened_at"), "closed_at": s.get("closed_at"),
                "module": {k: module.get(k) for k in ("rel_path", "format", "profile", "arch", "size", "sha256")}}

    def get(self, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            raise KeyError(session_id)
        s = self._load().get(session_id)
        if not s:
            raise KeyError(session_id)
        if s.get("closed_at"):
            raise WorkbenchError("closed", "This session was closed.", "Open the binary again with open_binary (annotations persist).")
        return s

    def list(self, include_closed: bool = False) -> list[dict[str, Any]]:
        out = []
        for sid, s in self._load().items():
            if s.get("closed_at") and not include_closed:
                continue
            try:
                m = self.studio.cases.get_module(s["module_id"])
            except KeyError:
                continue
            out.append(self._view(sid, s, m))
        return out

    def close(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            s = self.get(session_id)
            data = self._load()
            data[session_id]["closed_at"] = now_iso()
            self._save(data)
        b = self.backend()
        scope = f"{s['case_id']}:{s['module_id']}"
        with b.pool._lock:
            victims = [v for k, v in b.pool._sessions.items() if k[2] == scope]
        stopped = False
        for sess in victims:
            try:
                sess.close()
                stopped = True
            except Exception:
                pass
        return {"session_id": session_id, "closed": True, "rizin_stopped": stopped,
                "note": "Evidence and annotations are kept; open_binary again to continue."}

    # ------------------------------------------------------------------ operations
    def call(self, session_id: str, op: str, **kwargs: Any) -> tuple[OperationResult, dict[str, Any]]:
        s = self.get(session_id)
        fn = getattr(self.backend(), f"op_{op}", None)
        if fn is None:
            raise WorkbenchError("unavailable", f"operation {op} is not available in this build.", "")
        return fn(self.studio, case_id=s["case_id"], module_id=s["module_id"], **kwargs), s

    def decompile(self, session_id: str, function: str, decompiler: str = "rizin") -> tuple[OperationResult, dict[str, Any]]:
        if decompiler == "rizin":
            return self.call(session_id, "decompile", function=function)
        s = self.get(session_id)
        try:
            gh = self.studio.registry.get("ghidra")
        except (KeyError, AttributeError):
            gh = None
        if gh is None:
            from .backends.ghidra import GhidraBackend
            gh = GhidraBackend(self.studio.settings)
        probe = gh.tool_probe()
        if probe.availability.value not in ("installed", "usable", "verified"):
            raise WorkbenchError("unavailable", f"Ghidra headless is not available: {probe.detail}",
                                 "Use decompiler='rizin' (rz-ghidra), or set GHIDRA_INSTALL_DIR.")
        res = gh.op_decompile(self.studio, case_id=s["case_id"], module_id=s["module_id"], function=function)
        if res.ok:
            res.data["annotations_applied"] = False
            res.data["annotations_note"] = "Ghidra headless runs on the original file; annotations are applied to rizin output only."
        return res, s

    # ------------------------------------------------------------------ list helpers (filter + page; shared by MCP and app)
    @staticmethod
    def page(rows: list[Any], offset: int, limit: int) -> dict[str, Any]:
        return {"items": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit,
                "more": offset + limit < len(rows)}

    @staticmethod
    def contains(rows: list[dict[str, Any]], needle: Optional[str], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if not needle:
            return rows
        n = needle.lower()
        return [r for r in rows if any(n in str(r.get(k) or "").lower() for k in keys)]

    @staticmethod
    def compile_regex(pattern: str) -> re.Pattern[str]:
        """User regex over extracted strings, bounded: length cap and no nested quantifiers (catastrophic backtracking)."""
        if not pattern or len(pattern) > MAX_REGEX_CHARS:
            raise WorkbenchError("rejected", f"regex must be 1..{MAX_REGEX_CHARS} characters.", "")
        if re.search(r"\)[*+?{]", pattern) and re.search(r"[*+}]\)", pattern) or re.search(r"\\[1-9]", pattern):
            raise WorkbenchError("rejected", "nested quantifiers and backreferences are not allowed in string regexes.",
                                 "Simplify the pattern.")
        try:
            return re.compile(pattern)
        except re.error as e:
            raise WorkbenchError("rejected", f"invalid regex: {e}", "") from None
