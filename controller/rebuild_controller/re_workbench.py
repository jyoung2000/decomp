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


class EditRefused(WorkbenchError):
    """An annotation value that fails the grammar (name, C type, prototype, comment, declaration). ``field`` says which;
    the message says what is wrong and ends with a valid example."""

    def __init__(self, field: str, message: str):
        from .backends.re_annotations import EXAMPLES
        super().__init__("invalid_target", message, f"Fix the {field} and retry; a valid one looks like: {EXAMPLES[field]}")
        self.field = field


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

    def ghidra_backend(self) -> Any:
        try:
            gh = self.studio.registry.get("ghidra")
        except (KeyError, AttributeError):
            gh = None
        if gh is None:
            from .backends.ghidra import GhidraBackend
            gh = GhidraBackend(self.studio.settings)
        return gh

    def decompilers(self) -> list[dict[str, Any]]:
        """Which decompilers can run here (cheap probes, no analysis): rizin (rz-ghidra when loaded, else pseudo-code) and
        Ghidra headless (optional)."""
        out: list[dict[str, Any]] = []
        try:
            t = self.backend().tool()
        except Exception:
            t = None
        real = bool(t is not None and getattr(t, "ghidra_plugin", None) and not getattr(t, "static", False))
        out.append({"id": "rizin", "available": bool(t is not None and getattr(t, "version", None)), "real_decompiler": real,
                    "label": "rizin (rz-ghidra)" if real else "rizin (pseudo-code fallback)"})
        try:
            probe = self.ghidra_backend().tool_probe()
            ok = probe.availability.value in ("installed", "usable", "verified")
            out.append({"id": "ghidra", "available": ok, "real_decompiler": True, "label": f"Ghidra {probe.version or ''}".strip(),
                        "reason": None if ok else str(probe.detail or "")[:300]})
        except Exception as e:  # a broken Ghidra install must not break the pane
            out.append({"id": "ghidra", "available": False, "real_decompiler": True, "label": "Ghidra", "reason": str(e)[:300]})
        return out

    def decompile(self, session_id: str, function: str, decompiler: str = "rizin") -> tuple[OperationResult, dict[str, Any]]:
        if decompiler == "rizin":
            return self.call(session_id, "decompile", function=function)
        s = self.get(session_id)
        gh = self.ghidra_backend()
        probe = gh.tool_probe()
        if probe.availability.value not in ("installed", "usable", "verified"):
            raise WorkbenchError("unavailable", f"Ghidra headless is not available: {probe.detail}",
                                 "Use decompiler='rizin' (rz-ghidra), or install Ghidra and Temurin JDK 21 from the Tools page "
                                 "(or set GHIDRA_INSTALL_DIR).")
        res = gh.op_decompile(self.studio, case_id=s["case_id"], module_id=s["module_id"], function=function)
        if res.ok:
            res.data["annotations_applied"] = False
            res.data["annotations_note"] = "Ghidra headless runs on the original file; annotations are applied to rizin output only."
        return res, s

    # ------------------------------------------------------------------ annotation edits (R2 tools and R6 piece 5: one code path)
    def edit(self, session_id: str, action: str, *, author: str, kind: Optional[str] = None, target: Optional[str] = None,
             variable: Optional[str] = None, new_name: Optional[str] = None, prototype: Optional[str] = None,
             ctype: Optional[str] = None, addr: Optional[str] = None, text: Optional[str] = None,
             declaration: Optional[str] = None) -> tuple[OperationResult, dict[str, Any]]:
        """Rename / set a type / comment / declare types. Used by the MCP ``rename``, ``set_type``, ``add_comment`` and
        ``apply_struct`` tools (author ``model``: stored as model-proposed) and by the app's edit routes (author ``user``: a
        person's edit). The argument rules and the value grammar (``re_annotations.check_*``) are checked here, before rizin is
        touched, so both callers refuse the same input with the same message; the backend operation validates again before it
        builds a rizin command."""
        from .backends import re_annotations as ann
        if author not in ("user", "model"):
            raise WorkbenchError("refused", "edits are made by a person (user) or proposed by a model.", "")

        def need_target(what: str) -> str:
            if not target:
                raise WorkbenchError("rejected", "Provide exactly one of address or name.", f"Give the {what}.")
            return str(target)

        def check(what: str, value: Optional[str]) -> None:
            if value is None:
                return
            try:
                {"name": ann.check_ident, "type": ann.check_ctype, "prototype": ann.check_prototype, "comment": ann.check_comment,
                 "declaration": ann.check_declaration}[what](value)
            except ann.AnnotationError as e:
                raise EditRefused(what, str(e)) from None

        if action == "rename":
            if kind not in ("function", "global", "local"):
                raise WorkbenchError("rejected", "kind must be function, global or local.", "")
            if new_name is None:
                raise WorkbenchError("rejected", "new_name is required.", "Give the new name.")
            check("name", new_name)
            if kind == "local":
                if not variable:
                    raise WorkbenchError("rejected", "kind=local needs variable (the current variable name).", "Read get_function.variables.")
                check("name", variable)
                return self.call(session_id, "rename", kind=kind, target=variable, new_name=new_name,
                                 function=need_target("function (address or name)"), author=author)
            if variable:
                raise WorkbenchError("rejected", "variable is only valid with kind=local.", "Remove variable.")
            return self.call(session_id, "rename", kind=kind, target=need_target("address or name"), new_name=new_name, author=author)
        if action == "set_type":
            if kind == "function":
                if not prototype or variable or ctype:
                    raise WorkbenchError("rejected", "kind=function takes prototype only.", "Pass prototype.")
                check("prototype", prototype)
            elif kind == "local":
                if prototype or not variable or not ctype:
                    raise WorkbenchError("rejected", "kind=local takes variable and type.", "Pass variable and type.")
                check("name", variable)
                check("type", ctype)
            else:
                raise WorkbenchError("rejected", "kind must be function or local.", "")
            return self.call(session_id, "set_type", kind=kind, target=need_target("function (address or name)"), prototype=prototype,
                             variable=variable, ctype=ctype, author=author)
        if action == "comment":
            if not addr:
                raise WorkbenchError("rejected", "a comment needs an address.", "Give the address (0x...).")
            check("comment", text or "")
            return self.call(session_id, "add_comment", addr=addr, text=text or "", author=author)
        if action == "declare":
            check("declaration", declaration or "")
            return self.call(session_id, "apply_struct", declaration=declaration, author=author)
        raise WorkbenchError("rejected", f"unknown edit {action!r}.", "Use rename, set_type, comment or declare.")

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


# ---------------------------------------------------------------------- function list (shared by MCP list_functions and the app)
NATIVE_PROFILES = ("native_pe", "native_elf", "native_macho", "native_raw")
FUNCTION_SORTS = ("addr", "size", "name")
FUNCTION_FIELDS = ("addr", "name", "nbbs", "cc", "calltype", "signature", "noreturn")


def function_code_size(f: dict[str, Any]) -> int:
    """Code bytes of a rizin function. rizin's ``size`` is the address span (maxbound - minbound); one far-away block makes
    an 18-byte function look 23 KB (seen on pecli). ``realsz`` is the sum of its blocks."""
    return int(f.get("realsz") or f.get("size") or 0)


def shape_functions(functions: list[Any], contains: Optional[str], sort: str, offset: int, limit: int) -> dict[str, Any]:
    """Filter (case-insensitive name substring), sort (addr | size = code bytes, largest first | name) and page the rows of
    ``op_functions``. Each row: addr, name, nbbs, cc, calltype, signature, noreturn (when present), size (code bytes) and
    span (address span, only when it differs from size). Returns ``{functions, total, offset, returned, more}``."""
    if sort not in FUNCTION_SORTS:
        raise WorkbenchError("rejected", f"sort must be one of {', '.join(FUNCTION_SORTS)}.", "")
    rows = [f for f in functions if isinstance(f, dict)]
    if contains:
        n = contains.lower()
        rows = [f for f in rows if n in str(f.get("name") or "").lower()]
    key = {"addr": lambda f: f.get("offset") or 0, "size": lambda f: -function_code_size(f),
           "name": lambda f: str(f.get("name"))}[sort]
    rows.sort(key=key)
    slim = []
    for f in rows:
        row = {k: f.get(k) for k in FUNCTION_FIELDS if k in f}
        row["size"] = function_code_size(f)
        if f.get("size") and int(f["size"]) != row["size"]:
            row["span"] = int(f["size"])
        slim.append(row)
    page = slim[offset:offset + limit]
    return {"functions": page, "total": len(slim), "offset": offset, "returned": len(page), "more": offset + limit < len(slim)}


# ---------------------------------------------------------------------- one function (shared by the app's detail pane; R6 piece 2)
DISASM_PAGE_MAX = 2000                      # instructions per page in the app (MCP disassemble returns the whole function)
DECOMPILERS = ("rizin", "ghidra")


def function_header(info: dict[str, Any]) -> dict[str, Any]:
    """Header of the app's detail pane from ``op_function``'s ``function`` (``native.summarize_function``): code size as in the
    list (``realsz``), the address span only when it differs, and the first/last address of the code."""
    code = function_code_size(info)
    out = {k: info.get(k) for k in ("addr", "name", "signature", "cc", "calltype", "nbbs", "edges", "noreturn", "bits", "type",
                                    "stackframe", "n_callrefs", "n_datarefs", "callees") if info.get(k) is not None}
    out["size"] = code
    if info.get("size") and int(info["size"]) != code:
        out["span"] = int(info["size"])
    lo, hi = info.get("minbound"), info.get("maxbound")
    if isinstance(lo, int) and isinstance(hi, int):
        out["range"] = {"start": f"0x{lo:x}", "end": f"0x{hi:x}"}
    return out


def page_disasm(data: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    """Page the ``ops`` of ``op_disasm`` (the same rows the MCP ``disassemble`` tool returns for a whole function)."""
    if not 1 <= limit <= DISASM_PAGE_MAX or offset < 0:
        raise WorkbenchError("rejected", f"limit must be 1..{DISASM_PAGE_MAX} and offset >= 0.", "")
    d = dict(data.get("disasm") or {})
    ops = [o for o in d.get("ops") or [] if isinstance(o, dict)]
    page = ops[offset:offset + limit]
    return {"function": data.get("function"), "addr": data.get("addr"), "ops": page, "total": len(ops), "offset": offset,
            "returned": len(page), "more": offset + limit < len(ops)}


# ---------------------------------------------------------------------- cross references (shared by MCP xrefs_to/xrefs_from and the app; R6 piece 3)
XREF_ROW_FIELDS = ("from", "to", "addr", "type", "kind", "perm", "opcode", "fcn_addr", "fcn_name", "to_fcn_addr", "to_fcn_name",
                   "refname", "name", "string")
XREF_LIMIT_MAX = 1000
XREF_SCOPES = ("address", "function")


def hexify_refs(rows: list[Any]) -> list[dict[str, Any]]:
    """Rows of ``op_xrefs`` with a fixed field set and addresses as 0x-hex strings."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        x = {k: r[k] for k in XREF_ROW_FIELDS if k in r}
        for k in ("from", "to", "addr", "fcn_addr", "to_fcn_addr"):
            if isinstance(x.get(k), int):
                x[k] = "0x%x" % x[k]
        out.append(x)
    return out


def shape_xrefs(data: dict[str, Any], direction: str, limit: int) -> dict[str, Any]:
    """One direction (``to`` = references to it, ``from`` = references it makes) of ``op_xrefs`` output, at most ``limit``
    rows: ``{addr, direction, refs, total, more}``. ``total`` counts rows the backend dropped at its own cap too."""
    if direction not in ("to", "from") or not 1 <= int(limit) <= XREF_LIMIT_MAX:
        raise WorkbenchError("rejected", f"direction must be to or from and limit 1..{XREF_LIMIT_MAX}.", "")
    rows = hexify_refs(list(data.get(direction) or []))
    total = max(len(rows), int(data.get(f"{direction}_total") or 0))
    return {"addr": data.get("addr"), "direction": direction, "refs": rows[:limit], "total": total, "more": total > limit}


# ---------------------------------------------------------------------- module-level lists (shared by MCP strings / imports / exports /
# symbols / sections and the app's Strings, Imports, Exports and Sections views; R6 piece 4)
STRINGS_PAGE_MAX = 500
TABLE_PAGE_MAX = 1000
TABLE_FILTER_KEYS = ("name", "realname", "flagname", "libname")
SECTION_NAME = r"^[A-Za-z0-9_.$\-]{1,64}$"


def _filter_rows(rows: list[Any], contains: Optional[str], keys: tuple[str, ...]) -> list[Any]:
    if not contains:
        return rows
    n = contains.lower()
    return [r for r in rows if isinstance(r, dict) and any(n in str(r.get(k) or "").lower() for k in keys)]


def _page_rows(key: str, rows: list[Any], offset: int, limit: int) -> dict[str, Any]:
    page = rows[offset:offset + limit]
    return {key: page, "total": len(rows), "offset": offset, "returned": len(page), "more": offset + limit < len(rows)}


def shape_strings(data: dict[str, Any], *, contains: Optional[str] = None, regex: Optional[re.Pattern[str]] = None,
                  min_length: int = 4, section: Optional[str] = None, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Filter (minimum length, exact section, case-insensitive substring, bounded regex from ``ReWorkbench.compile_regex``) and
    page the rows of ``op_strings``; each row gets ``addr`` (0x-hex of ``vaddr``). Returns ``{strings, total, offset, returned,
    more, scanned, count_truncated}``. Rows keep the backend's fields: vaddr, paddr, length, section, type (encoding), string
    (clipped by the backend)."""
    rows = [x for x in data.get("strings") or [] if isinstance(x, dict) and (x.get("length") or 0) >= min_length]
    if section:
        rows = [x for x in rows if str(x.get("section") or "") == section]
    rows = _filter_rows(rows, contains, ("string",))
    if regex is not None:
        rows = [x for x in rows if regex.search(str(x.get("string") or ""))]
    rows = [dict(x) for x in rows]
    for x in rows:
        if isinstance(x.get("vaddr"), int):
            x["addr"] = "0x%x" % x["vaddr"]
    out = _page_rows("strings", rows, offset, limit)
    out["scanned"] = data.get("seen")
    out["count_truncated"] = bool(data.get("count_truncated"))
    return out


def string_sections(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Sections that hold strings, with how many (for the app's section filter)."""
    counts: dict[str, int] = {}
    for x in data.get("strings") or []:
        if isinstance(x, dict):
            k = str(x.get("section") or "")
            counts[k] = counts.get(k, 0) + 1
    return [{"section": k, "count": v} for k, v in sorted(counts.items())]


def shape_table(data: dict[str, Any], key: str, contains: Optional[str], offset: int, limit: int) -> dict[str, Any]:
    """Imports / exports / symbols of ``op_imports`` / ``op_exports`` / ``op_symbols``: name filter (name, realname, flagname,
    libname), ``addr`` = 0x-hex of ``vaddr`` (or ``plt`` for imports), paged: ``{<key>, total, offset, returned, more}``."""
    rows = [dict(x) if isinstance(x, dict) else x for x in _filter_rows(list(data.get(key) or []), contains, TABLE_FILTER_KEYS)]
    for x in rows:
        if isinstance(x, dict):
            for k in ("vaddr", "plt"):
                if isinstance(x.get(k), int) and x[k]:
                    x.setdefault("addr", "0x%x" % x[k])
    return _page_rows(key, rows, offset, limit)


def shape_sections(data: dict[str, Any]) -> dict[str, Any]:
    """``op_sections`` with vaddr/paddr as 0x-hex strings (sections and segments)."""
    out = dict(data)
    for grp in ("sections", "segments"):
        rows = []
        for x in data.get(grp) or []:
            if isinstance(x, dict):
                x = dict(x)
                for k in ("vaddr", "paddr"):
                    if isinstance(x.get(k), int):
                        x[k] = "0x%x" % x[k]
            rows.append(x)
        out[grp] = rows
    return out


def add_section_entropy(sections: list[dict[str, Any]], report: Optional[dict[str, Any]]) -> bool:
    """Copy per-section entropy (and the packer report's high-entropy threshold flag) from a packer report onto rizin's
    sections, matched by name and order (the report has RVAs, rizin absolute addresses). Returns True when any matched."""
    rep_secs = [s for s in (report or {}).get("sections") or [] if isinstance(s, dict)]
    if not rep_secs:
        return False
    from .backends.packer import HIGH_ENTROPY
    by_name: dict[str, list[dict[str, Any]]] = {}
    for s in rep_secs:
        by_name.setdefault(str(s.get("name") or ""), []).append(s)
    used: dict[str, int] = {}
    hit = False
    for s in sections:
        name = str(s.get("name") or "")
        i = used.get(name, 0)
        cand = by_name.get(name) or []
        if i < len(cand) and isinstance(cand[i].get("entropy"), (int, float)):
            s["entropy"] = float(cand[i]["entropy"])
            s["high_entropy"] = s["entropy"] >= HIGH_ENTROPY
            hit = True
        used[name] = i + 1
    return hit
